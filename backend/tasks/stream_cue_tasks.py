"""Cue pre-computation tasks, on the cue consumer (D50).

The task functions are the cue worker's composition root: they build the repositories, the
analyser and each run's configuration. A run also gets its progress sink (D77a, D89): a
``ProgressWriter`` on its own connection (design note 10, D94) and a coverage read on another
(I7), both closed or discarded when the run ends. A reported song gets no progress row (D79).
A run's row that a crash or a hard stop left RUNNING is failed when the worker starts (I1).
The progress row is telemetry, not the task's lifecycle, so the tasks do not use
``task_failure_telemetry``; ``reported_failures`` is their top boundary instead.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import structlog
from huey import crontab  # type: ignore[import-untyped]
from psycopg.rows import DictRow

from backend.config import get_settings
from backend.db.progress_writer import (
    Connect,
    ProgressWriter,
    bounded_coverage_read,
    progress_repository,
    writer_options,
)
from backend.db.sync_conn import connect_sync
from backend.domain.streaming import StreamTiming
from backend.domain.system import StorageUnavailableError
from backend.playout.cue_analysis import AnalyserConfig, analyse_batch, remove_listings
from backend.playout.liquidsoap_process import session_base_env
from backend.repositories.stream_cue_coverage import CoverageRead
from backend.services.repository_factory import RepositoryFactory
from backend.services.streaming.cue_precompute import (
    CueRunConfig,
    CueRunPorts,
    analyse_reported,
    run_cue_analysis,
)
from backend.services.streaming.cue_progress import (
    CueProgressPorts,
    CueProgressRows,
    end_leftover_cue_rows_with,
)
from backend.tasks.cue_huey_app import cue_huey

logger = structlog.get_logger()

ORPHAN_GRACE = timedelta(hours=24)  # D56: a second strike at least a day after the first
REPEAT_REPORT_AFTER = timedelta(hours=24)  # a lasting failure is reported once a day
RESUME_EXPIRES_S = 280  # a resume that waited behind an over-long run is dropped, not stacked
REQUEST_PRIORITY = 10  # above the resume, which has none: a waiting report runs first
REQUEST_EXPIRES_S = 3600  # a report older than an hour is dropped; the backlog covers it

_LOG_TIMESTAMP = re.compile(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} ")  # Liquidsoap's log lines
_OUTPUT_LINE = re.compile(r"output line \d+")  # where a protocol error was seen


def local_today() -> date:
    """Today's local date: the station day tune-in answers for (D62)."""
    return date.today()


def utc_now() -> datetime:
    return datetime.now(UTC)


def _cue_cache_dir(stream_work_dir: Path) -> Path:
    """The analyser's own cache folder: its script cache and each batch's file list."""
    return stream_work_dir / "cue-cache"


def _run_ports(conn: psycopg.Connection[DictRow], exe: Path, work_dir: Path) -> CueRunPorts:
    """The repositories, the connection's commit and the analyser callable, for one run or
    one report: ``exe`` is the Liquidsoap the caller found set (D51), ``work_dir`` the stream
    work folder. ``analyse_batch`` is looked up at call time, not bound at import, so the
    locked E1 tests' monkeypatch of it still takes effect."""
    repos = RepositoryFactory(conn)
    analyser = AnalyserConfig(exe=exe, cache_dir=_cue_cache_dir(work_dir))
    analyse = partial(analyse_batch, base_env=session_base_env(os.environ), config=analyser)
    return CueRunPorts(repos.streaming.cue_work, repos.streaming.cues, conn.commit, analyse)


@dataclass(frozen=True)
class ReportedFailure:
    """The last failure written to System Logs: its key (``failure_key``), and when."""

    message: str
    at: datetime

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:  # a naive time cannot be compared with utc_now()
            raise ValueError(f"ReportedFailure.at must be timezone-aware, got {self.at}")


class FailureMemory:
    """The last failure reported, for one consumer process.

    Module-level state, not a singleton: a restart forgets it and reports once more.
    """

    def __init__(self) -> None:
        self.last: ReportedFailure | None = None


FAILURES = FailureMemory()


def failure_is_new(last: ReportedFailure | None, message: str, now: datetime) -> bool:
    """Whether ``message`` at ``now`` earns an error: another failure, or a day later."""
    return last is None or last.message != message or now - last.at >= REPEAT_REPORT_AFTER


def failure_notes(error: BaseException) -> list[str]:
    """The notes ``add_note`` put on ``error`` (why the analyser was killed), oldest first.

    Logged as a field of their own: the log chain does not format ``exc_info``, so a
    traceback's notes never reach System Logs.
    """
    notes: object = getattr(error, "__notes__", None)
    return [str(note) for note in notes] if isinstance(notes, list) else []


def failure_key(error: BaseException) -> str:
    """What identifies a failure for the once-a-day rule: its type, message and notes.

    The key ignores what changes between runs of the same failure: Liquidsoap's log
    timestamps, the protocol's output line numbers, and the rest of a multi-line message's
    first line, where the analyser's log tail starts mid-line.
    """
    first, *rest = str(error).splitlines() or [""]
    heading = first.partition(": ")[0] if rest else first
    key = "\n".join([f"{type(error).__name__}: {heading}", *rest, *failure_notes(error)])
    return _OUTPUT_LINE.sub("output line N", _LOG_TIMESTAMP.sub("", key))


@contextmanager
def reported_failures(task_name: str) -> Iterator[None]:
    """The task's top boundary: one error, with its traceback, per distinct failure a day.

    It never re-raises: Huey would log every raise too, every 5 minutes. A later failure with
    the same key goes to debug; a success forgets the last failure. The notes are a field of
    their own, so they reach System Logs; ``exc_info`` is for the DEBUG console.
    """
    try:
        yield
    except Exception as error:  # noqa: BLE001 - task top boundary; the progress row is telemetry
        key = failure_key(error)
        now = utc_now()
        detail = f"{type(error).__name__}: {error}"
        notes = failure_notes(error)
        if failure_is_new(FAILURES.last, key, now):
            logger.error(
                "stream_cue_task_failed", task=task_name, error=detail, notes=notes, exc_info=True
            )
            FAILURES.last = ReportedFailure(key, now)
        else:
            logger.debug("stream_cue_task_failed", task=task_name, error=detail, notes=notes)
    else:
        FAILURES.last = None


@cue_huey.on_startup()  # type: ignore[untyped-decorator]
def remove_stale_cue_listings() -> None:
    """Delete the batch file lists a hard-killed consumer left; no batch runs yet (-w 1)."""
    remove_listings(_cue_cache_dir(get_settings().stream_work_dir))


def telemetry_connect(database_url: str) -> Connect:
    """Opens a progress-row connection: its own, autocommit, not waiting for the disk, and
    bounded (D94, ``writer_options()``)."""
    return partial(
        connect_sync, database_url, autocommit=True, connect_timeout=2, options=writer_options()
    )


@cue_huey.on_startup()  # type: ignore[untyped-decorator]
def end_leftover_cue_rows() -> None:
    """Fail the cue run rows a crash or a hard stop left RUNNING (I1); no run is live yet
    (-w 1). A database that cannot be reached is logged, and the worker starts anyway."""
    try:
        with progress_repository(telemetry_connect(get_settings().database_url)) as repo:
            end_leftover_cue_rows_with(repo)
    except StorageUnavailableError as error:
        logger.warning("cue_progress_leftover_unended", error=str(error))


def cue_progress_ports(
    writer: ProgressWriter, coverage: CoverageRead, run_id: str
) -> CueProgressPorts:
    """One run's progress ports: the writer's write, the bounded count, the UTC clock (I5)
    and the run's own row id (M12)."""
    return CueProgressPorts(write=writer.write, coverage=coverage, clock=utc_now, run_id=run_id)


@cue_huey.task()  # type: ignore[untyped-decorator]
def stream_cue_analysis_task() -> None:
    """One bounded cue pre-computation run, whenever LIQUIDSOAP_PATH is set (D51), with its
    progress row (D89). Opening the writer's connection waits for the first row."""
    settings = get_settings()
    exe = settings.liquidsoap_path
    if exe is None:
        logger.debug("stream_cue_analysis_off")
        return
    url = settings.database_url
    writer = ProgressWriter(telemetry_connect(url))
    progress = CueProgressRows(cue_progress_ports(writer, bounded_coverage_read(url), uuid4().hex))
    with (
        reported_failures("stream_cue_analysis_task"),
        closing(writer),
        connect_sync(url, autocommit=False) as conn,
    ):
        ports = replace(_run_ports(conn, exe, settings.stream_work_dir), progress=progress)
        run_cue_analysis(ports, CueRunConfig(today=local_today(), steady=time.monotonic))


@cue_huey.task(  # type: ignore[untyped-decorator]
    priority=REQUEST_PRIORITY, expires=REQUEST_EXPIRES_S
)
def stream_cue_request_task(file_id: str) -> None:
    """D79: analyse one reported song's audio ahead of the backlog, on the cue consumer (D50:
    never beside a run)."""
    settings = get_settings()
    exe = settings.liquidsoap_path
    if exe is None:
        logger.debug("stream_cue_analysis_off")
        return
    with (
        reported_failures("stream_cue_request_task"),
        connect_sync(settings.database_url, autocommit=False) as conn,
    ):
        ports = _run_ports(conn, exe, settings.stream_work_dir)
        analyse_reported(ports, StreamTiming(), UUID(file_id))


def request_cue_analysis(file_id: UUID) -> None:
    """The stream service's way to the cue owner (D79): queue one report on the cue
    consumer. A short SQLite write: call it off the event loop."""
    stream_cue_request_task(str(file_id))


@cue_huey.periodic_task(  # type: ignore[untyped-decorator]
    crontab(minute="*/5"), expires=RESUME_EXPIRES_S
)
def stream_cue_analysis_resume() -> None:
    """Continue the backlog in bounded runs (D63)."""
    stream_cue_analysis_task.call_local()


@cue_huey.periodic_task(crontab(minute="0", hour="4"))  # type: ignore[untyped-decorator]
def stream_cue_prune_task() -> None:
    """The daily two-strike prune of cue rows no library file carries (D56)."""
    # Not gated on LIQUIDSOAP_PATH (D51): it only tidies existing rows, harmless without it.
    with (
        reported_failures("stream_cue_prune_task"),
        connect_sync(get_settings().database_url, autocommit=False) as conn,
    ):
        RepositoryFactory(conn).streaming.cues.prune_orphans(utc_now(), ORPHAN_GRACE)
        conn.commit()
