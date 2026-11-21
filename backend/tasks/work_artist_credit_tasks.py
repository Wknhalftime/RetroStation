"""3b D1's run-once step: every performance with no credit credits its work's artist (work
merges phase 3b PR 2, SPEC D1, D17's Q3, D20; AUD-R078).

After 3a's build is done (its marker), each performance with no credit gets one, its work's
artist from 0055's seed (none for a placeholder or an unseeded work; 3b D38). Batches commit as
they go; its own done-marker ends it, and the seed is dropped in the marker's transaction (D49):
the one run-once step that changes the schema. It makes
no MusicBrainz call and moves no file. Nothing queues it: the cutover and the gate run it by
name, after the build and before any merge. On the live data it credits almost nothing, since
the build already credits each performance it makes (D20).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog

from backend.config import get_settings
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, TaskType
from backend.domain.library import PERFORMANCES_BUILT_SETTING, WORK_ARTISTS_CREDITED_SETTING
from backend.domain.system import UserSetting
from backend.services.performance_service import credit_work_artist
from backend.services.repository_factory import RepositoryFactory
from backend.tasks._task_run import TaskLifecycleMessages, TaskRunConfig, task_run
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

BATCH_SIZE = 500


@huey.task()  # type: ignore[untyped-decorator]
def credit_work_artists_task() -> dict[str, object]:
    """Credit every uncredited performance, or do nothing once the marker is set; what it did."""
    settings = get_settings()
    with connect_sync(settings.database_url) as conn:
        user_settings = RepositoryFactory(conn).user_settings
        if user_settings.get(WORK_ARTISTS_CREDITED_SETTING) is not None:
            return {"done": True, "credited": 0}
        if user_settings.get(PERFORMANCES_BUILT_SETTING) is None:
            logger.info("work_artist_credits_waiting", waiting_for=PERFORMANCES_BUILT_SETTING)
            return {"done": False, "credited": 0}

    credited = [0]
    config = TaskRunConfig(
        task_type=TaskType.WORK_ARTIST_CREDITS,
        log_category=LogCategory.SCAN,
        messages=TaskLifecycleMessages(
            started="work_artist_credits_started",
            completed="work_artist_credits_completed",
            failed="work_artist_credits_failed",
        ),
        read_progress=lambda: (credited[0], credited[0]),
    )
    with task_run(settings.database_url, config) as handle:
        handle.report_running(progress_data={"processed": 0, "total": 0, "current_item": ""})
        handle.log_started(details={})
        with connect_sync(settings.database_url) as conn:
            repos = RepositoryFactory(conn)
            after: UUID | None = None
            while batch := repos.performances.uncredited(after, BATCH_SIZE):
                for performance_id, artist in batch:
                    if credit_work_artist(performance_id, artist, repos.performances):
                        credited[0] += 1
                conn.commit()
                after = batch[-1][0]
                handle.report_running(
                    progress_data={"processed": credited[0], "total": 0, "current_item": ""}
                )
            # D49: the seed's last reader is done; it goes with the marker, in one commit.
            repos.performances.drop_seed()
            marker = UserSetting(WORK_ARTISTS_CREDITED_SETTING, datetime.now(UTC).isoformat())
            repos.user_settings.upsert(marker)
            conn.commit()
        result: dict[str, object] = {"done": True, "credited": credited[0]}
        handle.set_completed(
            progress_data={"processed": credited[0], "total": credited[0], **result},
            details=result,
        )
    logger.info("work_artist_credits_complete", **result)
    return result
