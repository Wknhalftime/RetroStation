"""S6's sweep: delete every work with no files that nothing points at (work merges phase 3b
PR 4; SPEC S6, D7, D15; design C53, §6.9; AUD-R045, AUD-R080).

No marker: it runs after 3b's merges and again after phase 4's, and each run finds only what
emptied since. Nothing queues it: the cutover runs it by name after the merge list. The works it
keeps are listed on the merge list (D15).
"""

from __future__ import annotations

import structlog

from backend.config import get_settings
from backend.db.sync_conn import connect_sync
from backend.services.empty_work_sweep import sweep_empty_works
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def sweep_empty_works_task() -> dict[str, object]:
    """One sweep; how many works it deleted and kept."""
    settings = get_settings()
    with connect_sync(settings.database_url) as conn:
        result = sweep_empty_works(RepositoryFactory(conn).works)
        conn.commit()
    outcome: dict[str, object] = {"deleted": result.deleted, "kept": result.kept}
    logger.info("empty_works_swept", **outcome)
    return outcome
