"""Daily retention for ``progress_tracking``.

Every task envelope writes a row and nothing removed one, so the table kept every run since
April. The ``/ws`` feed reads the whole table twice a second, so its size is the feed's cost.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from huey import crontab  # type: ignore[import-untyped]

from backend.config import get_settings
from backend.db.sync_conn import commit_or_unavailable, connect_sync
from backend.repositories.task_progress_retention import TaskProgressRetention
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

PROGRESS_RETENTION = timedelta(days=14)
"""How long a finished row stays. The UI shows a finished row for seconds; two weeks keeps a
recent run's row around to read when something went wrong."""


def prune_finished_progress(retention: TaskProgressRetention, now: datetime) -> int:
    """Delete the rows that ended more than ``PROGRESS_RETENTION`` before *now*."""
    return retention.delete_finished_before(now - PROGRESS_RETENTION)


@huey.periodic_task(crontab(minute="30", hour="4"))  # type: ignore[untyped-decorator]
def progress_prune_task() -> None:
    """Prune finished progress rows once a day."""
    with connect_sync(get_settings().database_url, autocommit=False) as conn:
        deleted = prune_finished_progress(
            RepositoryFactory(conn).task_progress, datetime.now(tz=UTC)
        )
        commit_or_unavailable(conn)
    logger.info("progress_rows_pruned", deleted=deleted)
