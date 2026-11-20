"""S5's run-once task: the global unique index on ``performances.recording_mbid`` (work merges
phase 3b PR 4; SPEC S5, E6; design C40; AUD-R080).

Builds the index with ``CREATE UNIQUE INDEX CONCURRENTLY`` once no recording MBID is under two
works, never from a migration. Nothing queues it: the cutover runs it by name after the merge
list. A run while duplicates remain does nothing and names them; once the index stands, a run
does nothing.
"""

from __future__ import annotations

import psycopg
import structlog
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.services.recording_index_service import build_when_unique
from backend.services.repository_factory import RepositoryFactory, recording_index_repo
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def build_recording_index_task() -> dict[str, object]:
    """Build the index if no duplicate is left; what it did."""
    settings = get_settings()
    # One autocommit connection: no transaction of ours stays open for the build to wait on.
    with psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row) as conn:
        outcome = build_when_unique(
            RepositoryFactory(conn).performances, recording_index_repo(conn)
        )
    result: dict[str, object] = {
        "done": outcome.done,
        "built": outcome.built,
        "remaining": list(outcome.remaining),
    }
    logger.info("recording_index_run", **result)
    return result
