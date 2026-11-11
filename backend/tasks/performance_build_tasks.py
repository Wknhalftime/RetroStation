"""The run-once build of performances (phase 3a PR 2, SPEC S2, E1, E5; design §6.3 step 3).

After 2a's credit backfill is done (its marker), every file on a work, present or missing, is
given a performance of its work in file-id order (``assign_performance``, S2's rule). Batches
commit as they go, so a run that stopped resumes with the files still without one (E5). Its
own done-marker ends it for good: from then on the writers keep performances (S3). Nothing
queues it: the cutover and the gate run it by name.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog

from backend.config import get_settings
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, TaskType
from backend.domain.library import ARTIST_CREDITS_BACKFILLED_SETTING, PERFORMANCES_BUILT_SETTING
from backend.domain.system import UserSetting
from backend.services.performance_service import assign_performance
from backend.services.repository_factory import RepositoryFactory
from backend.tasks._task_run import TaskLifecycleMessages, TaskRunConfig, task_run
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

BATCH_SIZE = 500


@huey.task()  # type: ignore[untyped-decorator]
def build_performances_task() -> dict[str, object]:
    """Build every file's performance, or do nothing once the marker is set; what it did."""
    settings = get_settings()
    with connect_sync(settings.database_url) as conn:
        user_settings = RepositoryFactory(conn).user_settings
        if user_settings.get(PERFORMANCES_BUILT_SETTING) is not None:
            return {"done": True, "files": 0}
        if user_settings.get(ARTIST_CREDITS_BACKFILLED_SETTING) is None:
            logger.info("performances_build_waiting", waiting_for=ARTIST_CREDITS_BACKFILLED_SETTING)
            return {"done": False, "files": 0}

    built = [0]
    config = TaskRunConfig(
        task_type=TaskType.PERFORMANCES_BUILD,
        log_category=LogCategory.SCAN,
        messages=TaskLifecycleMessages(
            started="performances_build_started",
            completed="performances_build_completed",
            failed="performances_build_failed",
        ),
        read_progress=lambda: (built[0], built[0]),
    )
    with task_run(settings.database_url, config) as handle:
        handle.report_running(progress_data={"processed": 0, "total": 0, "current_item": ""})
        handle.log_started(details={})
        with connect_sync(settings.database_url) as conn:
            repos = RepositoryFactory(conn)
            after: UUID | None = None
            while batch := repos.performances.files_waiting(after, BATCH_SIZE):
                for file_id in batch:
                    assign_performance(file_id, repos.performances)
                conn.commit()
                built[0] += len(batch)
                after = batch[-1]
                handle.report_running(
                    progress_data={"processed": built[0], "total": 0, "current_item": ""}
                )
            marker = UserSetting(PERFORMANCES_BUILT_SETTING, datetime.now(UTC).isoformat())
            repos.user_settings.upsert(marker)
            conn.commit()
        result: dict[str, object] = {"done": True, "files": built[0]}
        handle.set_completed(
            progress_data={"processed": built[0], "total": built[0], **result}, details=result
        )
    logger.info("performances_build_complete", **result)
    return result
