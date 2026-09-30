import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path

import psycopg
import structlog
from fastapi import FastAPI, Request, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout, TooManyRequests

from backend.config import Settings, callback_base_url, get_settings
from backend.db.migrations import run_migrations
from backend.db.pool import close_pool, init_pool
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.playable_schedule import PgPlayableScheduleRepository
from backend.db.repositories.user_settings import PgUserSettingRepository
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
from backend.routers import listen, stream_internal
from backend.routers.v1 import router as v1_router
from backend.services.streaming.bookmarks import BookmarkStore
from backend.services.streaming.service import (
    ReposFactory,
    StreamPorts,
    StreamRepos,
    StreamService,
    StreamServiceConfig,
)
from backend.services.streaming.watchdog import run_freeze_watchdog
from backend.websocket import websocket_endpoint

logger = structlog.get_logger()

_WATCHDOG_INTERVAL_S = 5.0

type EngineStarter = Callable[[SessionEndpoint], Awaitable[RunningEngine]]


@dataclass(frozen=True)
class StreamingRuntime:
    """What the lifespan holds while streaming is on, and releases at shutdown."""

    service: StreamService
    close_job: Callable[[], None]
    watchdog: asyncio.Task[None]


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


def _stream_repos(database_url: str) -> ReposFactory:
    """One connection per use, holding the repositories the stream service reads."""

    @contextmanager
    def opened() -> Iterator[StreamRepos]:
        with connect_sync(database_url) as conn:
            yield StreamRepos(
                stations=PgBroadcastStationRepository(conn),
                settings=PgUserSettingRepository(conn),
                schedule=PgPlayableScheduleRepository(conn),
            )

    return opened


def _stream_service(settings: Settings, start_engine: EngineStarter, logs: Path) -> StreamService:
    """The stream service, reading PostgreSQL and calling back on the API's own bind (D24)."""
    return StreamService(
        StreamPorts(_stream_repos(settings.database_url), start_engine, datetime.now),
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
    from backend.playout.windows_job import KillOnCloseJob

    try:
        job = KillOnCloseJob()
    except OSError as error:
        _engine_unavailable("the job that ends engines with the app was refused", str(error))
        return None
    start_engine = partial(start_ready_engine, job.assign, base_env, engine=engine)
    service = _stream_service(settings, start_engine, work / "logs")
    return StreamingRuntime(service, job.close, _start_watchdog(service))


async def stop_streaming(runtime: StreamingRuntime | None) -> None:
    """Stop the watchdog, close every session, then the job (its children die with it)."""
    if runtime is None:
        return
    runtime.watchdog.cancel()
    await asyncio.wait({runtime.watchdog})
    try:
        runtime.service.close_all()
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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time task progress broadcast."""
    await websocket_endpoint(websocket)
