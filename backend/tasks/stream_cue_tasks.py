"""Cue pre-computation tasks, on the cue consumer (D50).

The task functions are the cue worker's composition root: they build the repositories, the
analyser and each run's configuration. There are no progress rows (D59), so the tasks do not
use ``task_failure_telemetry``; ``reported_failures`` is their top boundary instead.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path

import structlog
from huey import crontab  # type: ignore[import-untyped]

from backend.config import get_settings
from backend.db.sync_conn import connect_sync
from backend.playout.cue_analysis import AnalyserConfig, analyse_batch, remove_listings
from backend.playout.liquidsoap_process import session_base_env
from backend.services.repository_factory import RepositoryFactory
from backend.services.streaming.cue_precompute import (
    CueRunConfig,
    CueRunPorts,
    run_cue_analysis,
)
from backend.tasks.cue_huey_app import cue_huey

logger = structlog.get_logger()

ORPHAN_GRACE = timedelta(hours=24)  # D56: a second strike at least a day after the first
REPEAT_REPORT_AFTER = timedelta(hours=24)  # a lasting failure is reported once a day


def local_today() -> date:
    """Today's local date: the station day tune-in answers for (D62)."""
    return date.today()


def utc_now() -> datetime:
    return datetime.now(UTC)


def _cue_cache_dir(stream_work_dir: Path) -> Path:
    """The analyser's own cache folder: its script cache and each batch's file list."""
    return stream_work_dir / "cue-cache"


@dataclass(frozen=True)
class ReportedFailure:
    """The last failure written to System Logs: its heading (``failure_heading``), and when."""

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


def failure_heading(error: BaseException) -> str:
    """What identifies a failure: its type and its message up to the first ``": "``.

    An ``AnalyserError`` carries the tail of Liquidsoap's log, whose lines are timestamped,
    so the whole message differs every run; its heading ("cue analyser exit code 3") does not.
    """
    return f"{type(error).__name__}: {str(error).partition(': ')[0]}"


@contextmanager
def reported_failures(task_name: str) -> Iterator[None]:
    """The task's top boundary: one error, with its traceback, per distinct failure a day.

    It never re-raises: Huey would log every raise too, every 5 minutes. A later failure of
    the same heading goes to debug; a success forgets the last failure.
    """
    try:
        yield
    except Exception as error:  # noqa: BLE001 - task top boundary (D59: no progress row)
        heading = failure_heading(error)
        now = utc_now()
        detail = f"{type(error).__name__}: {error}"
        if failure_is_new(FAILURES.last, heading, now):
            # exc_info keeps the notes analyse_batch adds (why the analyser was killed).
            logger.error("stream_cue_task_failed", task=task_name, error=detail, exc_info=True)
            FAILURES.last = ReportedFailure(heading, now)
        else:
            logger.debug("stream_cue_task_failed", task=task_name, error=detail)
    else:
        FAILURES.last = None


@cue_huey.on_startup()  # type: ignore[untyped-decorator]
def remove_stale_cue_listings() -> None:
    """Delete the batch file lists a hard-killed consumer left; no batch runs yet (-w 1)."""
    remove_listings(_cue_cache_dir(get_settings().stream_work_dir))


@cue_huey.task()  # type: ignore[untyped-decorator]
def stream_cue_analysis_task() -> None:
    """One bounded cue pre-computation run, whenever LIQUIDSOAP_PATH is set (D51)."""
    settings = get_settings()
    if settings.liquidsoap_path is None:
        logger.debug("stream_cue_analysis_off")
        return
    with (
        reported_failures("stream_cue_analysis_task"),
        connect_sync(settings.database_url, autocommit=False) as conn,
    ):
        repos = RepositoryFactory(conn)
        analyser = AnalyserConfig(
            exe=settings.liquidsoap_path,
            cache_dir=_cue_cache_dir(settings.stream_work_dir),
        )
        analyse = partial(analyse_batch, base_env=session_base_env(os.environ), config=analyser)
        run_cue_analysis(
            CueRunPorts(repos.streaming.cue_work, repos.streaming.cues, conn.commit, analyse),
            CueRunConfig(today=local_today(), steady=time.monotonic),
        )


@cue_huey.periodic_task(crontab(minute="*/5"))  # type: ignore[untyped-decorator]
def stream_cue_analysis_resume() -> None:
    """Continue the backlog in bounded runs (D63)."""
    stream_cue_analysis_task.call_local()


@cue_huey.periodic_task(crontab(minute="0", hour="4"))  # type: ignore[untyped-decorator]
def stream_cue_prune_task() -> None:
    """The daily two-strike prune of cue rows no library file carries (D56)."""
    with (
        reported_failures("stream_cue_prune_task"),
        connect_sync(get_settings().database_url, autocommit=False) as conn,
    ):
        RepositoryFactory(conn).streaming.cues.prune_orphans(utc_now(), ORPHAN_GRACE)
        conn.commit()
