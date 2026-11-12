"""The Credit picks proposals (phase 3a PR 3, SPEC S8, D4; design C57, M27; AUD-R039).

After PR 2's build is done (its marker), propose a stored performance for every matched line
whose credit picks a non-default one (``credit_pick_service.propose_all``). It has no marker of
its own, so it can run again (4.4); a re-run keeps Lance's decisions. It changes nothing that
plays. Nothing queues it: the cutover and the gate run it by name.
"""

from __future__ import annotations

import structlog

from backend.config import get_settings
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, TaskType
from backend.domain.library import PERFORMANCES_BUILT_SETTING
from backend.services.credit_pick_service import propose_all
from backend.services.repository_factory import RepositoryFactory
from backend.tasks._task_run import TaskLifecycleMessages, TaskRunConfig, task_run
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def propose_credit_picks_task() -> dict[str, object]:
    """Propose Credit picks once performances are built; what it did."""
    settings = get_settings()
    with connect_sync(settings.database_url) as conn:
        if RepositoryFactory(conn).user_settings.get(PERFORMANCES_BUILT_SETTING) is None:
            logger.info("credit_picks_waiting", waiting_for=PERFORMANCES_BUILT_SETTING)
            return {"done": False, "waiting": 0}

    waiting = [0]
    config = TaskRunConfig(
        task_type=TaskType.CREDIT_PICK_PROPOSALS,
        log_category=LogCategory.SCAN,
        messages=TaskLifecycleMessages(
            started="credit_pick_proposals_started",
            completed="credit_pick_proposals_completed",
            failed="credit_pick_proposals_failed",
        ),
        read_progress=lambda: (waiting[0], waiting[0]),
    )
    with task_run(settings.database_url, config) as handle:
        handle.report_running(progress_data={"processed": 0, "total": 0, "current_item": ""})
        handle.log_started(details={})
        with connect_sync(settings.database_url) as conn:
            waiting[0] = propose_all(RepositoryFactory(conn).credit_picks)
            conn.commit()
        result: dict[str, object] = {"done": True, "waiting": waiting[0]}
        handle.set_completed(
            progress_data={"processed": waiting[0], "total": waiting[0], **result}, details=result
        )
    logger.info("credit_pick_proposals_complete", **result)
    return result
