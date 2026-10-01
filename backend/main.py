import asyncio
import os
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path

import psycopg
import structlog
from fastapi import FastAPI, Request, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.rows import DictRow
from psycopg_pool import PoolTimeout, TooManyRequests

from backend.config import Settings, callback_base_url, get_settings
from backend.db.migrations import run_migrations
from backend.db.pool import close_pool, init_pool
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.playable_schedule import PgPlayableScheduleRepository
from backend.db.repositories.user_settings import PgUserSettingRepository
from backend.db.stream_reads import ReadBounds, bounded_connection
from backend.db.sync_conn import connect_sync
from backend.logging_config import configure_logging
from backend.playout.assets import ensure_stream_assets
from backend.playout.liquidsoap_process import (
    SESSION_SCRIPT,
    EngineConfig,
    RunningEngine,
    ScriptCacheError,
    SessionEndpoint,
    prune_session_logs,
    session_base_env,
    start_ready_engine,
    warm_script_cache,
)
from backend.routers import listen, radio, stream_internal
from backend.routers.v1 import router as v1_router
from backend.services.streaming.bookmarks import BookmarkStore
from backend.services.streaming.cue_reports import CueReporter, NoCueReports
from backend.services.streaming.service import (
    ReposFactory,
    StreamPorts,
    StreamRepos,
    StreamService,
    StreamServiceConfig,
)
from backend.services.streaming.watchdog import run_freeze_watchdog
from backend.tasks.stream_cue_tasks import request_cue_analysis
from backend.websocket import websocket_endpoint

logger = structlog.get_logger()

_WATCHDOG_INTERVAL_S = 5.0
REPORT_FLUSH_S = 2.0
"""D87(b): shutdown waits this long for the cue reporter's waiting reports to be sent."""
STREAM_READ_BOUNDS = ReadBounds(
    connect_timeout_s=5, lock_timeout_ms=2_000, statement_timeout_ms=10_000
)
"""D88: the stream service's reads give up on a lock after 2 s (the realistic cause), on any
statement after 10 s (a backstop far above a cold day read's ~1.7 s, D40) and on connecting
after 5 s; a tune-in then answers 503 unavailable rather than hanging."""

type EngineStarter = Callable[[SessionEndpoint], Awaitable[RunningEngine]]


@dataclass(frozen=True)
class StreamingRuntime:
    """What the lifespan holds while streaming is on, and releases at shutdown."""

    service: StreamService
    close_job: Callable[[], None]
    watchdog: asyncio.Task[None]
    cue_reports: NoCueReports
    """Where a song played without cues was reported (D79); flushed at shutdown (D87(b))."""


def _engine_unavailable(reason: str, output: str = "") -> None:
    """Streaming stays off; /listen answers 503 ``unavailable`` (D34)."""
    logger.error("stream_engine_unavailable", reason=reason, output=output)


def _log_prune_failure(path: Path, error: OSError) -> None:
    logger.warning("stream_log_prune_failed", path=str(path), error=str(error))


def _prune_logs(logs: Path) -> None:
    """Keep the newest session logs (D35); a pruning error is logged, never raised (D46)."""
    try:
        prune_session_logs(logs, on_error=_log_prune_failure)
    except OSError as error:  # the folder could not be listed, or a log failed otherwise
        _log_prune_failure(logs, error)


def _make_work_folders(work: Path) -> bool:
    """Create the assets, cache and logs folders; False (logged) if one cannot be made."""
    try:
        for folder in (work / "assets", work / "cache", work / "logs"):
            folder.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        _engine_unavailable("the streaming work folders could not be created", str(error))
        return False
    return True


async def _prepared_engine(
    ffmpeg: str, exe: Path, work: Path, base_env: dict[str, str]
) -> EngineConfig | None:
    """The engine, its filler and intro made and its script cache warm; None (logged) if
    either cannot be prepared (spec: Engine "the app must warm it at startup"; D34)."""
    try:
        assets = ensure_stream_assets(ffmpeg, work / "assets")
    except (subprocess.SubprocessError, OSError) as error:
        _engine_unavailable("the filler and intro could not be generated", str(error))
        return None
    engine = EngineConfig(
        exe=exe,
        script=SESSION_SCRIPT,
        cache_dir=work / "cache",
        filler=assets.filler,
        intro_sfx=assets.static_intro,
    )
    try:
        await asyncio.to_thread(warm_script_cache, base_env, engine)
    except (ScriptCacheError, subprocess.TimeoutExpired, OSError) as error:
        _engine_unavailable("Liquidsoap's script cache could not be built", str(error))
        return None
    return engine


def _opened_repos(
    connect: Callable[[], AbstractContextManager[psycopg.Connection[DictRow]]],
) -> ReposFactory:
    """A ``ReposFactory`` over whatever connection ``connect`` opens, one per use."""

    @contextmanager
    def opened() -> Iterator[StreamRepos]:
        with connect() as conn:
            yield StreamRepos(
                stations=PgBroadcastStationRepository(conn),
                settings=PgUserSettingRepository(conn),
                schedule=PgPlayableScheduleRepository(conn),
            )

    return opened


def _stream_repos(database_url: str) -> ReposFactory:
    """One connection per use, holding the repositories the stream service reads; each read
    is bounded (D88), so a locked or unreachable database fails it with ``StreamReadError``."""
    return _opened_repos(lambda: bounded_connection(database_url, STREAM_READ_BOUNDS))


def _cue_reread_repos(database_url: str) -> ReposFactory:
    """One connection per use, short-lived (D85, review M4): a stuck database frees the
    re-read's slots instead of holding them, so a cold day read (``_stream_repos``, ~1.7 s,
    D40) keeps its own, longer-lived connections. Autocommit: the re-read is one read, so no
    COMMIT round trip after ``search_path`` or on exit (review M7)."""
    options = "-c statement_timeout=2000 -c lock_timeout=1000"
    return _opened_repos(
        lambda: connect_sync(database_url, connect_timeout=2, options=options, autocommit=True)
    )


def build_stream_ports(settings: Settings, start_engine: EngineStarter) -> StreamPorts:
    """What the stream service is wired to: PostgreSQL, the engine start, two clocks and the
    no-cue cue reports port.

    D47: the wall clock only places listeners; the monotonic clock times everything else,
    and the now-playing delay waits on the loop's monotonic clock.
    D79: no-cue reports go to the cue consumer's queue (it runs whenever LIQUIDSOAP_PATH is
    set, D51; streaming needs it too).
    """
    return StreamPorts(
        _stream_repos(settings.database_url),
        start_engine,
        datetime.now,
        time.monotonic,
        cue_reread_repos=_cue_reread_repos(settings.database_url),
        sleep=asyncio.sleep,
        cue_reports=CueReporter(request_cue_analysis),
    )


def _stream_service(ports: StreamPorts, settings: Settings, logs: Path) -> StreamService:
    """The stream service, reading PostgreSQL and calling back on the API's own bind (D24)."""
    return StreamService(
        ports,
        BookmarkStore(),
        StreamServiceConfig(callback_base_url(settings.server_host, settings.server_port), logs),
    )


def _report_watchdog_end(watchdog: asyncio.Task[None]) -> None:
    """The watchdog only ends by cancellation at shutdown; any other end is an error."""
    if watchdog.cancelled():
        return
    error = watchdog.exception()
    logger.error(
        "stream_watchdog_died",
        message="frozen sessions are no longer stopped",
        error=repr(error),
        exc_info=error,
    )


def _start_watchdog(service: StreamService) -> asyncio.Task[None]:
    watchdog = asyncio.create_task(run_freeze_watchdog(service.stop_frozen, _WATCHDOG_INTERVAL_S))
    watchdog.add_done_callback(_report_watchdog_end)
    return watchdog


async def start_streaming(settings: Settings) -> StreamingRuntime | None:
    """Streaming when enabled and able; None, streaming off, otherwise (I5, D34, D35)."""
    if not settings.stream_enabled:
        return None
    if sys.platform != "win32":
        _engine_unavailable("streaming runs on Windows only (D5)")
        return None
    if settings.liquidsoap_path is None:
        _engine_unavailable("LIQUIDSOAP_PATH (.env) is not set")
        return None
    work = settings.stream_work_dir
    if not _make_work_folders(work):
        return None
    _prune_logs(work / "logs")
    base_env = session_base_env(os.environ)
    engine = await _prepared_engine(settings.ffmpeg_path, settings.liquidsoap_path, work, base_env)
    if engine is None:
        return None
    # Lazy: windows_job raises ImportError off Windows, and this line is reached only on win32.
    from backend.playout.windows_job import KillOnCloseJob

    try:
        job = KillOnCloseJob()
    except OSError as error:
        _engine_unavailable("the job that ends engines with the app was refused", str(error))
        return None
    start_engine = partial(start_ready_engine, job.assign, base_env, engine=engine)
    ports = build_stream_ports(settings, start_engine)
    service = _stream_service(ports, settings, work / "logs")
    return StreamingRuntime(service, job.close, _start_watchdog(service), ports.cue_reports)


async def _flush_cue_reports(reports: NoCueReports) -> None:
    """Wait at most ``REPORT_FLUSH_S`` for the reports still waiting to be sent (D87(b)); past
    that, log and move on without cancelling a report in flight (the backlog still covers the
    rest). Either way, close a wired ``CueReporter`` without waiting (carried from Task 6a,
    review I1): it drops the reports still waiting and ignores later ones, and its worker is a
    daemon thread, so a hung request never delays the app's exit. The close runs even when
    the flush is cancelled (re-review N1).
    """
    try:
        flushing = asyncio.ensure_future(reports.drained())
        done, _ = await asyncio.wait({flushing}, timeout=REPORT_FLUSH_S)
        if not done:
            logger.warning("stream_cue_reports_unflushed")
            flushing.cancel()
            await asyncio.wait({flushing})
    finally:
        if isinstance(reports, CueReporter):
            reports.close()


async def stop_streaming(runtime: StreamingRuntime | None) -> None:
    """Stop the watchdog, close every session, flush and close the cue reports, then the job
    (its children die with it). The flush, the reporter's close and the job's close each run
    even when an earlier step raises, and that error still propagates (re-review N1)."""
    if runtime is None:
        return
    runtime.watchdog.cancel()
    await asyncio.wait({runtime.watchdog})
    try:
        runtime.service.close_all()
    finally:
        try:
            await _flush_cue_reports(runtime.cue_reports)
        finally:
            runtime.close_job()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, database_url=settings.database_url)

    if settings.airwave_token == "dev-token":
        logger.warning(
            "security_warning",
            message="Using default dev-token. Set AIRWAVE_TOKEN in .env for production.",
        )

    pool = init_pool(settings.database_url)
    await pool.open()

    # CRITICAL: RETROSTATION_SKIP_BOOT_MIGRATIONS is a TEST-ONLY escape hatch.
    # Production deployments must never set it. The test harness sets it from
    # tests/routers/conftest.py only, where session-scope fixtures have already
    # applied migrations against the same DB URL. The flag is honored ONLY when
    # pytest is loaded in this process; otherwise the flag is ignored and the
    # critical log line fires, so a misconfigured prod deployment cannot boot
    # against an unmigrated schema even if the env var leaks in.
    skip_flag = os.getenv("RETROSTATION_SKIP_BOOT_MIGRATIONS") == "1"
    in_pytest = "pytest" in sys.modules

    if skip_flag and in_pytest:
        logger.warning(
            "boot_migrations_skipped",
            message="RETROSTATION_SKIP_BOOT_MIGRATIONS=1; skipping lifespan migrations.",
        )
    else:
        if skip_flag and not in_pytest:
            logger.critical(
                "boot_migrations_skip_flag_ignored",
                message=(
                    "RETROSTATION_SKIP_BOOT_MIGRATIONS=1 but pytest is not loaded; "
                    "ignoring flag and running migrations."
                ),
            )
        with psycopg.connect(settings.database_url) as conn:
            run_migrations(conn)
            conn.commit()

    app.state.server_host = settings.server_host
    streaming = await start_streaming(settings)
    app.state.stream_service = None if streaming is None else streaming.service

    try:
        yield
    finally:
        await stop_streaming(streaming)
        await close_pool()


app = FastAPI(title="RetroStation", lifespan=lifespan)

# CORS: allow the Vite dev server to talk to the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _pool_saturation_handler(request: Request, exc: Exception) -> JSONResponse:
    """Translate connection-pool saturation into HTTP 503.

    PoolTimeout fires when a waiter exceeds the 30s default client timeout;
    TooManyRequests fires when the wait queue is already at max_waiting.
    Both mean the service is overloaded (not broken), so 503 with
    Retry-After is the honest signal and matches the claim in
    backend.db.pool.init_pool.
    """
    logger.warning(
        "db_pool_saturated",
        exception=type(exc).__name__,
        path=request.url.path,
        method=request.method,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Database pool saturated; please retry."},
        headers={"Retry-After": "1"},
    )


app.add_exception_handler(PoolTimeout, _pool_saturation_handler)
app.add_exception_handler(TooManyRequests, _pool_saturation_handler)

app.include_router(v1_router)
app.include_router(stream_internal.router)
app.include_router(listen.router)
app.include_router(radio.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time task progress broadcast."""
    await websocket_endpoint(websocket)
