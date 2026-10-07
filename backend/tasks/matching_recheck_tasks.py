"""Targeted re-check of undecided matching (AUD-R022 D1; spec 2026-10-05 §4.2).

The two producers:
- ``mb_enrichment_task`` hands off ``rematch_undecided_task("changed")``;
- the Re-run Matching button queues ``"all"`` through ``queue_recheck_all``.

One run:
- reads the watermark;
- rewinds the wave's undecided artists and songs to pending, and commits;
- hands every playlist with pending work to ``artist_matching_task``, which hands on to
  identity matching.
"""

from __future__ import annotations

import sqlite3
from uuid import UUID

import structlog

from backend.config import get_settings
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, RecheckScope, TaskType
from backend.domain.matching import RecheckNotQueuedError
from backend.services.matching_recheck_service import (
    playlists_with_pending_work,
    recheck_names,
    rewind_wave,
)
from backend.services.repository_factory import RepositoryFactory
from backend.tasks._enqueue_chain import enqueue_or_log
from backend.tasks._task_run import TaskLifecycleMessages, TaskRunConfig, task_run
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def rematch_undecided_task(scope: str) -> dict[str, int]:
    """Re-check undecided matching for one wave, then fan out to artist matching.

    ``scope`` is ``"changed"``: the artists of files indexed or gone missing since the newest
    COMPLETED re-check. Or it is ``"all"``. It is a plain ``str``, because a StrEnum task
    parameter is an EV07 hit. The run sits inside ``task_run``, so its COMPLETED row is the next
    run's watermark.
    """
    settings = get_settings()
    task_id = ""
    playlist_ids: list[UUID] = []
    summary: dict[str, int] = {}

    config = TaskRunConfig(
        task_type=TaskType.MATCHING_RECHECK,
        log_category=LogCategory.MATCHING,
        messages=TaskLifecycleMessages(
            started="matching_recheck_started",
            completed="matching_recheck_completed",
            failed="matching_recheck_failed",
        ),
        read_progress=lambda: (0, 0),
    )

    with task_run(settings.database_url, config) as handle:
        task_id = handle.task_id
        recheck_scope = RecheckScope(scope)
        handle.report_running(
            progress_data={"processed": 0, "total": 0, "scope": recheck_scope.value}
        )
        # handle.started_at was taken before any query. Once this run's COMPLETED row is
        # written, it is the next run's watermark. FAILED and RUNNING rows never count.
        watermark = handle.progress_repo.last_completed_started_at(TaskType.MATCHING_RECHECK)
        handle.log_started(
            details={
                "scope": recheck_scope.value,
                "watermark": watermark.isoformat() if watermark is not None else None,
            }
        )
        with connect_sync(settings.database_url) as conn:
            repos = RepositoryFactory(conn)
            names = recheck_names(recheck_scope, watermark, repos.library_files)
            counts = rewind_wave(names, repos.broadcast_artists, repos.broadcast_identities)
            # Committed before the fan-out: the matching worker reads the rewound statuses.
            conn.commit()
            playlist_ids = sorted(
                playlists_with_pending_work(repos.broadcast_artists, repos.broadcast_identities),
                key=str,
            )
        summary = {
            "artists_rewound": counts.artists,
            "songs_rewound": counts.songs,
            "playlists": len(playlist_ids),
        }
        details: dict[str, object] = {
            "scope": recheck_scope.value,
            "wave_names": None if names is None else len(names),
            **summary,
        }
        handle.set_completed(
            progress_data={"processed": 1, "total": 1, **details},
            details=details,
        )

    logger.info("matching_recheck_task_complete", task_id=task_id, scope=scope, **summary)

    # Hand each playlist with pending work to artist matching, even when the wave was empty:
    # items left pending by a failed downstream run are retried this way. Guarded per
    # playlist (AUD-R012 (1), AUD-R014) on a fresh autocommit connection, after the envelope,
    # as library_enrichment_task does. The run is already COMPLETED; a refused enqueue is
    # logged on its task_id.
    from backend.tasks.artist_matching_tasks import artist_matching_task

    with connect_sync(settings.database_url, autocommit=True) as log_conn:
        sys_log_repo = PgSystemLogRepository(log_conn)
        for playlist_id in playlist_ids:
            pid = str(playlist_id)
            enqueue_or_log(
                lambda: artist_matching_task(pid),  # noqa: B023 - called at once
                task_name="artist_matching_task",
                caller_task_id=task_id,
                log_category=LogCategory.MATCHING,
                sys_log_repo=sys_log_repo,
            )

    return summary


def queue_recheck_all() -> None:
    """Queue a re-check of everything undecided (the Re-run Matching button).

    Raises:
        RecheckNotQueuedError: the Huey store refused the enqueue. The ``sqlite3`` error is
            translated here, at the adapter layer, so the router maps a domain error only.
    """
    try:
        rematch_undecided_task(RecheckScope.ALL.value)
    except sqlite3.Error as exc:
        raise RecheckNotQueuedError(f"the re-check could not be queued: {exc}") from exc
