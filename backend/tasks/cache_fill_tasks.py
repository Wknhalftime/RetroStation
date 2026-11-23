"""The MusicBrainz cache fill's tasks (phase 4.1 PR 2; SPEC S3, S4, S7; D6, D8; M1-M11;
AUD-R014, AUD-R017, AUD-R042, AUD-R091).

- ``mb_cache_fill_task()`` starts a run. Nothing queues it: the cutover runbook queues the first
  run by name, and every later one is queued the same way, on Lance's word (D6). It refuses, and
  logs ``mb_cache_fill_refused``, while a run is in progress: a fill batch RUNNING, or one
  waiting in Huey's queue (between batches no row is RUNNING). Otherwise it queues the first
  batch and writes no row of its own.
- ``mb_cache_fill_batch_task(run)`` is one batch, at Huey priority -10, below every other task:
  at most 100 calls, none started after 110 s, inside ``task_run`` (one row per batch, written
  before each call, D4). ``run`` is the run's state as one JSON string (``FillRun``). After the
  envelope it queues a ``rematch_names_task`` for each tier the run left with names filled
  (S7), then the next batch while capped work remains, each through ``enqueue_or_log``.
- PR 2b (D10, D11; AUD-R093): a batch asks through a one-attempt view of the client, pauses
  with ``sleep`` after a refusal, and reads the wall clock through ``utc_now``, which also stamps
  a run's ``started_at``: a run hands on no batch once its 2 hours are up.
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic, sleep
from typing import Any

import structlog

from backend.config import get_settings
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.sync_conn import commit_or_unavailable, connect_sync
from backend.domain.cache_fill import FillRun
from backend.domain.enums import LogCategory, TaskType
from backend.services.cache_fill_constants import FILL_BATCH_PRIORITY, FILL_LIMITS
from backend.services.cache_fill_service import (
    BatchOutcome,
    BatchPorts,
    FillSources,
    alias_writer,
    plan_run,
    run_batch,
)
from backend.services.mb_client import MusicBrainzApiClient
from backend.services.repository_factory import RepositoryFactory
from backend.tasks._enqueue_chain import enqueue_or_log
from backend.tasks._task_run import TaskLifecycleMessages, TaskRunConfig, TaskRunHandle, task_run
from backend.tasks.huey_app import huey
from backend.tasks.matching_recheck_tasks import rematch_names_task

logger = structlog.get_logger()

BATCH_TASK = "mb_cache_fill_batch_task"


def utc_now() -> datetime:
    """The wall clock a run's ``started_at`` and its 2 hours are read on (D11); a test seam."""
    return datetime.now(UTC)


@huey.task()  # type: ignore[untyped-decorator]
def mb_cache_fill_task() -> None:
    """Start a fill run (D6), unless one is in progress."""
    settings = get_settings()
    with connect_sync(settings.database_url, autocommit=True) as conn:
        running = [
            task.task_id
            for task in PgTaskProgressRepository(conn).list_running()
            if task.task_type is TaskType.MB_CACHE_FILL
        ]
        queued = sum(1 for task in huey.pending() if task.name == BATCH_TASK)
        if running or queued:
            logger.warning("mb_cache_fill_refused", running=running, queued=queued)
            return
        run = FillRun.start(utc_now())
        refused: list[bool] = []
        enqueue_or_log(
            lambda: mb_cache_fill_batch_task(run.to_json()),
            task_name=BATCH_TASK,
            caller_task_id=f"mb_cache_fill:{run.started_at}",
            log_category=LogCategory.ENRICHMENT,
            sys_log_repo=PgSystemLogRepository(conn),
            on_failure=lambda: refused.append(True),
        )
        if not refused:
            logger.info("mb_cache_fill_started", run_started_at=run.started_at)


class _Shown:
    """The batch's last reported calls made and planned, for a FAILED row."""

    def __init__(self, handle: TaskRunHandle) -> None:
        self._handle = handle
        self.processed = 0
        self.total = 0

    def report(self, progress_data: dict[str, Any]) -> None:
        self.processed = int(progress_data["processed"])
        self.total = int(progress_data["total"])
        self._handle.report_running(progress_data=progress_data)


@huey.task(priority=FILL_BATCH_PRIORITY)  # type: ignore[untyped-decorator]
def mb_cache_fill_batch_task(run: str) -> dict[str, int]:
    """One batch of a fill run; ``run`` is its ``FillRun`` as JSON."""
    settings = get_settings()
    state = FillRun.from_json(run)
    shown: list[_Shown] = []
    config = TaskRunConfig(
        task_type=TaskType.MB_CACHE_FILL,
        log_category=LogCategory.ENRICHMENT,
        messages=TaskLifecycleMessages(
            started="mb_cache_fill_batch_started",
            completed="mb_cache_fill_batch_completed",
            failed="mb_cache_fill_batch_failed",
        ),
        read_progress=lambda: (shown[0].processed, shown[0].total) if shown else (0, 0),
    )
    with task_run(settings.database_url, config) as handle:
        shown.append(_Shown(handle))
        # The row exists from the start, label only, so the guard (D6) sees the batch while it
        # plans; the first call's row replaces it.
        handle.report_running(
            progress_data={
                "processed": 0,
                "total": 0,
                "run_started_at": state.started_at,
                **state.totals(),
            }
        )
        handle.log_started(details={"run_started_at": state.started_at})
        outcome = _batch(settings.database_url, settings.mb_cache_ttl_days, state, shown[0])
        handle.set_completed(progress_data=outcome.progress, details=outcome.progress)

    # The re-checks of the tiers the run left, then the next batch (AUD-R014), on a fresh
    # autocommit connection after the envelope: the batch is already COMPLETED. Made here in the
    # task's own body, so the event graph (AUD-R019) sees the chain's hand-off to itself.
    with connect_sync(settings.database_url, autocommit=True) as log_conn:
        sys_log_repo = PgSystemLogRepository(log_conn)
        for _tier, names in outcome.rechecks:
            enqueue_or_log(
                lambda: rematch_names_task(names),  # noqa: B023 - called at once
                task_name="rematch_names_task",
                caller_task_id=handle.task_id,
                log_category=LogCategory.ENRICHMENT,
                sys_log_repo=sys_log_repo,
            )
        next_run = outcome.next_run
        if next_run is not None:
            enqueue_or_log(
                lambda: mb_cache_fill_batch_task(next_run.to_json()),
                task_name=BATCH_TASK,
                caller_task_id=handle.task_id,
                log_category=LogCategory.ENRICHMENT,
                sys_log_repo=sys_log_repo,
            )
    calls: dict[str, int] = outcome.progress["calls"]
    return {"calls": sum(calls.values()), "rechecks": len(outcome.rechecks)}


def _batch(database_url: str, ttl_days: int, state: FillRun, shown: _Shown) -> BatchOutcome:
    """Plan the run's calls and make this batch's, on one transactional connection."""
    limits = FILL_LIMITS
    with connect_sync(database_url) as conn:
        repos = RepositoryFactory(conn)
        sources = FillSources(
            work=repos.cache_fill_work,
            cache=repos.musicbrainz_cache,
            write_aliases=alias_writer(repos.artists, repos.artist_names),
            name_max_len=get_settings().broadcast_name_max_len,
        )
        plan = plan_run(sources, state, limits)
        with MusicBrainzApiClient(PgMusicBrainzCacheRepository(conn), ttl_days=ttl_days) as client:
            # D10: one request a call; the client's own 3 tries stay for every other caller.
            fill_client = client.with_attempts(limits.attempts)
            ports = BatchPorts(
                client=fill_client,
                commit=lambda: commit_or_unavailable(conn),
                clock=monotonic,
                report=shown.report,
                sleep=sleep,
                now=utc_now,
                requests=lambda: fill_client.requests_sent,
            )
            outcome = run_batch(plan, state, limits, ports)
        commit_or_unavailable(conn)
    return outcome
