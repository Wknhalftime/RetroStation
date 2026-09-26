"""Library hash backfill — fill in the content hashes a first scan deferred.

library_scan_task queues library_hash_backfill_task after a first scan, and
library_hash_backfill_resume restarts a run cut short by a crash or a worker
restart. It is one long task like the full scan: hashing is disk-bound and
Huey runs a single worker (-w 1).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import structlog
from huey import crontab  # type: ignore[import-untyped]

from backend.config import get_settings
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.task_progress import TaskProgressRepository
from backend.services.hash_backfill_service import HashBackfillBatch, backfill_hash_batch
from backend.services.repository_factory import RepositoryFactory
from backend.tasks._error_boundary import task_failure_telemetry
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

BATCH_SIZE = 200
# A RUNNING backfill row not updated for this long belongs to a dead run.
# Matches backend.websocket's STALE_THRESHOLD_MINUTES, the stale reaper
# actually in use.
LIVENESS_WINDOW = timedelta(minutes=10)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class BackfillRunConfig:
    """Identity and pacing of one backfill run."""

    run_id: str
    batch_size: int = BATCH_SIZE
    clock: Callable[[], datetime] = _utcnow


@dataclass(frozen=True)
class BackfillProgress:
    """Running totals for one backfill run, as the progress bar shows them."""

    run_id: str
    total: int
    started_at: datetime
    hashed: int = 0
    changed: int = 0
    unreadable: int = 0
    current_path: str = ""

    @property
    def processed(self) -> int:
        return self.hashed + self.changed + self.unreadable

    def after(self, batch: HashBackfillBatch) -> BackfillProgress:
        return replace(
            self,
            hashed=self.hashed + batch.hashed,
            changed=self.changed + batch.changed,
            unreadable=self.unreadable + batch.unreadable,
            current_path=batch.last_path or self.current_path,
        )

    def as_task_progress(self, now: datetime, *, done: bool) -> TaskProgress:
        return TaskProgress(
            task_id=self.run_id,
            task_type=TaskType.HASH_BACKFILL,
            status=TaskStatus.COMPLETED if done else TaskStatus.RUNNING,
            progress_data={
                "processed": self.processed,
                "total": self.total,
                "hashed": self.hashed,
                "changed": self.changed,
                "unreadable": self.unreadable,
                "current_path": self.current_path,
            },
            started_at=self.started_at,
            updated_at=now,
            completed_at=now if done else None,
        )


def backfill_is_live(progress_repo: TaskProgressRepository, now: datetime) -> bool:
    """Whether another backfill run has reported progress within LIVENESS_WINDOW."""
    return any(
        task.task_type == TaskType.HASH_BACKFILL and now - task.updated_at < LIVENESS_WINDOW
        for task in progress_repo.list_running()
    )


def run_hash_backfill(
    file_repo: LibraryFileRepository,
    progress_repo: TaskProgressRepository,
    commit: Callable[[], None],
    config: BackfillRunConfig,
) -> BackfillProgress | None:
    """Hash every PRESENT row that has no hash yet, one committed batch at a time.

    Returns the run's totals, or None when there was nothing to hash or
    another run is live.
    """
    now = config.clock()
    if backfill_is_live(progress_repo, now):
        logger.info("hash_backfill_already_running")
        return None
    total = file_repo.count_unhashed()
    if total == 0:
        return None

    progress = BackfillProgress(run_id=config.run_id, total=total, started_at=now)
    cursor: str | None = None
    while True:
        batch = backfill_hash_batch(file_repo, after_path=cursor, limit=config.batch_size)
        if batch.exhausted:
            break
        cursor = batch.last_path
        progress = progress.after(batch)
        # A row this run can never hash (unreadable past its tags, or its
        # stat keeps moving with no watcher running) leaves count_unhashed()
        # above zero forever, so the periodic resume starts a run every 5
        # minutes. Post progress only once the run has actually hashed
        # something, so a run that hashes nothing leaves no trace for the
        # UI to show as a perpetually running backfill.
        if progress.hashed > 0:
            progress_repo.upsert(progress.as_task_progress(config.clock(), done=False))
        commit()

    if progress.hashed > 0:
        progress_repo.upsert(progress.as_task_progress(config.clock(), done=True))
        # Only log completion when the run actually did something. A row this
        # run can never hash keeps count_unhashed() above zero forever, so
        # the periodic resume starts a new run every 5 minutes; logging an
        # unconditional info here would spam system_logs with a no-op event
        # every 5 minutes for as long as that row stays unhashable.
        logger.info(
            "hash_backfill_complete",
            hashed=progress.hashed, changed=progress.changed, unreadable=progress.unreadable,
        )
    commit()
    return progress


@huey.task()  # type: ignore[untyped-decorator]
def library_hash_backfill_task() -> None:
    """Fill in deferred content hashes. Does nothing when none are missing."""
    with (
        task_failure_telemetry(TaskType.HASH_BACKFILL, LogCategory.SCAN) as run_id,
        connect_sync(get_settings().database_url, autocommit=False) as conn,
    ):
        repos = RepositoryFactory(conn)
        run_hash_backfill(
            repos.library_files, repos.task_progress, conn.commit,
            BackfillRunConfig(run_id=run_id),
        )


@huey.periodic_task(crontab(minute="*/5"))  # type: ignore[untyped-decorator]
def library_hash_backfill_resume() -> None:
    """Restart a backfill that a crash or worker restart cut short."""
    library_hash_backfill_task.call_local()
