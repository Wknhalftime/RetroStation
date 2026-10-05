"""
Library watcher tasks — periodic polling and targeted smart scan.

The poll task runs every 4 minutes, diffs folder hashes, and enqueues a
targeted scan for changed folders. The scan task processes changed folders
using smart per-folder diffing and chains into enrichment.
"""

from __future__ import annotations

import contextlib
import traceback
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import psycopg
import structlog
from huey import crontab  # type: ignore[import-untyped]

from backend.config import get_settings
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import EnrichmentStatus, LogCategory, LogLevel, TaskStatus, TaskType
from backend.domain.system import SystemLog, TaskProgress
from backend.repositories.library_folder_staging import LibraryFolderHashStaging
from backend.repositories.library_folders import LibraryFolderRepository
from backend.services.folder_hash_service import diff_tree
from backend.services.grouping_service import assign_work
from backend.services.library_scan_service import scan_folder_incrementally
from backend.services.repository_factory import RepositoryFactory
from backend.tasks._enqueue_chain import enqueue_or_log
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


# A scan that has neither committed nor cleared its staged hashes after
# this long is dead (worker killed, task lost from the queue), and its
# folders stop counting as in flight. If it was merely slow, the cost is
# one redundant rescan, and a rescan of unchanged files is only stat()s.
STAGED_HASH_TTL = timedelta(hours=1)


def detect_changed_folders(
    folder_repo: LibraryFolderRepository,
    staging: LibraryFolderHashStaging,
    root_path: str,
    now: datetime,
) -> tuple[list[str], list[tuple[UUID, str]]]:
    """Folders under *root_path* whose files changed, minus those a live scan holds."""
    staging.clear_stale_staged_hashes(now - STAGED_HASH_TTL)
    in_flight_ids = staging.get_folders_with_staged_hashes()
    return diff_tree(root_path, folder_repo, in_flight_ids)


@huey.periodic_task(crontab(minute="*/4"))  # type: ignore[untyped-decorator]
def library_watcher_poll() -> None:
    """Poll the library directory for changes every 4 minutes."""
    settings = get_settings()

    with connect_sync(
        settings.database_url,
        autocommit=False,
    ) as conn:
        repos = RepositoryFactory(conn)

        _setting = repos.user_settings.get("local_path_prefix")
        root_path = _setting.value if _setting is not None else None
        if not root_path:
            return

        changed, pending = detect_changed_folders(
            repos.library_folders,
            repos.library_folders,
            root_path,
            datetime.now(UTC),
        )
        conn.commit()

        if not changed:
            return

        # Stage pending hashes with a task ID.  pending is already
        # (folder_id, new_hash) tuples from diff_tree — no extra query.
        task_id = uuid.uuid4().hex
        repos.library_folders.stage_hashes(pending, task_id)
        conn.commit()

        logger.info(
            "watcher_poll_changes_detected",
            changed=len(changed),
            task_id=task_id,
        )

        # Every changed folder is scanned individually. diff_tree reports
        # only folders whose own files changed and the scan is non-recursive,
        # so there is nothing to collapse — a parent never stands in for a
        # child.
        #
        # Guarded hand-off (AUD-R012 (1), AUD-R014). The poll has no run of its
        # own, so a failure is logged under the staging task_id. On failure the
        # staged folders are released, so the next 4-minute poll retries them
        # instead of waiting out STAGED_HASH_TTL. The log goes through its own
        # autocommit connection, so it survives even if the release fails.
        def release_staged_folders() -> None:
            repos.library_folders.clear_staged_hashes(task_id)
            conn.commit()

        with connect_sync(settings.database_url, autocommit=True) as log_conn:
            enqueue_or_log(
                lambda: library_scan_files_task(changed, task_id),
                task_name="library_scan_files_task",
                caller_task_id=task_id,
                log_category=LogCategory.SCAN,
                sys_log_repo=PgSystemLogRepository(log_conn),
                on_failure=release_staged_folders,
            )


@huey.task()  # type: ignore[untyped-decorator]
def library_scan_files_task(folder_paths: list[str], task_id: str) -> None:
    """Smart scan of specific folders, then chain into enrichment."""
    settings = get_settings()
    scan_task_id = uuid.uuid4().hex
    task_started_at = datetime.now(UTC)
    total_written = 0

    progress_conn = None
    library_conn = None

    try:
        # Autocommit connection for progress tracking and system logs
        progress_conn = connect_sync(
            settings.database_url,
            autocommit=True,
        )
        progress_repo = PgTaskProgressRepository(progress_conn)
        sys_log_repo = PgSystemLogRepository(progress_conn)

        # Data connection
        library_conn = connect_sync(
            settings.database_url,
            autocommit=False,
        )

        repos = RepositoryFactory(library_conn)

        # Advisory lock — prevent overlapping scans
        lock_key = folder_paths[0] if folder_paths else "watcher"
        lock_row = library_conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)::bigint) AS acquired",
            (lock_key,),
        ).fetchone()
        if not lock_row or not lock_row["acquired"]:
            logger.warning("scan_lock_held", paths=folder_paths)
            # Clear staged hashes so the next poll re-detects these folders
            # instead of skipping them via the in-flight overlap guard.
            repos.library_folders.clear_staged_hashes(task_id)
            library_conn.commit()
            return

        # Initial progress
        progress_repo.upsert(
            TaskProgress(
                task_id=scan_task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.RUNNING,
                progress_data={
                    "processed": 0,
                    "total": len(folder_paths),
                    "current_path": "",
                    "source": "watcher",
                },
                started_at=task_started_at,
                updated_at=task_started_at,
            )
        )

        sys_log_repo.create(
            SystemLog(
                category=LogCategory.SCAN,
                level=LogLevel.INFO,
                message="watcher_scan_started",
                trace_id=scan_task_id,
                details={"folder_count": len(folder_paths)},
            )
        )

        for idx, folder_path in enumerate(folder_paths, start=1):
            result = scan_folder_incrementally(
                folder_path=Path(folder_path),
                file_repo=repos.library_files,
                quarantine_repo=repos.library_quarantine,
            )
            total_written += result.files_written
            library_conn.commit()

            # Grouping pass for every file in this folder without a work_id —
            # not only when this visit wrote something. A file can lose its
            # work_id without its content changing (an interrupted full scan
            # did exactly that to thousands of rows), and the next visit is
            # the place to repair it. One indexed SELECT per folder; a no-op
            # loop when everything is already grouped.
            folder_files = repos.library_files.get_by_folder_path(
                folder_path,
            )
            for lf in folder_files:
                if lf.work_id is not None:
                    continue
                # Per-file commit so a single file's failure never rolls
                # back previously successful grouping writes in this
                # folder. Narrow to expected per-file failure modes;
                # mirrors library_scan_tasks.py's grouping loop. Logic
                # bugs propagate to the task boundary instead of being
                # logged per-file and continuing.
                try:
                    grouping = assign_work(
                        lf,
                        artist_repo=repos.artists,
                        work_repo=repos.works,
                        library_file_repo=repos.library_files,
                        recording_repo=repos.recordings,
                        song_master_repo=repos.song_masters,
                    )
                    if grouping:
                        repos.library_files.update_work_id(
                            lf.id,
                            grouping.work_id,
                        )
                        if grouping.recording_id:
                            repos.library_files.update_recording_link(
                                lf.id,
                                grouping.recording_id,
                                EnrichmentStatus.PENDING,
                            )
                    library_conn.commit()
                except (psycopg.Error, ValueError, OSError):
                    library_conn.rollback()
                    logger.warning(
                        "watcher_grouping_failed",
                        file_id=str(lf.id),
                        exc_info=True,
                    )

            progress_repo.upsert(
                TaskProgress(
                    task_id=scan_task_id,
                    task_type=TaskType.SCAN,
                    status=TaskStatus.RUNNING,
                    progress_data={
                        "processed": idx,
                        "total": len(folder_paths),
                        "current_path": folder_path,
                        "source": "watcher",
                        "files_written": total_written,
                    },
                    started_at=task_started_at,
                    updated_at=datetime.now(UTC),
                )
            )

            logger.info(
                "watcher_scan_folder_complete",
                folder=folder_path,
                written=result.files_written,
                skipped=result.files_skipped,
                missing=result.files_missing,
                reappeared=result.files_reappeared,
                relocated=result.files_relocated,
                quarantined=result.quarantined,
                quarantine_cleared=result.quarantine_cleared,
                unreadable=result.folder_unreadable,
            )

        # A move seen as two folder events pairs up whichever folder came first.
        from backend.tasks.library_scan_tasks import reconcile_missing_after_scan

        reconcile_missing_after_scan(library_conn, repos)

        # Commit staged hashes on success
        repos.library_folders.commit_staged_hashes(task_id)
        library_conn.commit()

        # Mark completed
        progress_repo.upsert(
            TaskProgress(
                task_id=scan_task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.COMPLETED,
                progress_data={
                    "processed": len(folder_paths),
                    "total": len(folder_paths),
                    "source": "watcher",
                    "files_written": total_written,
                },
                started_at=task_started_at,
                updated_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )

        sys_log_repo.create(
            SystemLog(
                category=LogCategory.SCAN,
                level=LogLevel.INFO,
                message="watcher_scan_completed",
                trace_id=scan_task_id,
                details={
                    "processed": len(folder_paths),
                    "total": len(folder_paths),
                    "files_written": total_written,
                },
            )
        )

        logger.info(
            "watcher_scan_complete",
            folders=len(folder_paths),
            total_written=total_written,
        )

        # Chain into enrichment if files were written. Guarded (AUD-R012
        # (1)): this scan's own run already reported COMPLETED above,
        # so a downstream enqueue failure must not retroactively flip it to
        # FAILED. The caller owns the handoff and logs the failure on its
        # own task_id instead.
        if total_written > 0:
            from backend.tasks.library_enrichment_tasks import (
                library_enrichment_task,
            )

            enqueue_or_log(
                library_enrichment_task,
                task_name="library_enrichment_task",
                caller_task_id=scan_task_id,
                log_category=LogCategory.SCAN,
                sys_log_repo=sys_log_repo,
            )

    except Exception as exc:
        if library_conn is not None:
            with contextlib.suppress(Exception):
                library_conn.rollback()

            # Clean up staged hashes so the next poll re-detects changes
            with contextlib.suppress(Exception):
                RepositoryFactory(library_conn).library_folders.clear_staged_hashes(task_id)
                library_conn.commit()

        if progress_conn is not None:
            with contextlib.suppress(Exception):
                PgTaskProgressRepository(progress_conn).upsert(
                    TaskProgress(
                        task_id=scan_task_id,
                        task_type=TaskType.SCAN,
                        status=TaskStatus.FAILED,
                        progress_data={"error": str(exc), "source": "watcher"},
                        started_at=task_started_at,
                        updated_at=datetime.now(UTC),
                        completed_at=datetime.now(UTC),
                    )
                )
            with contextlib.suppress(Exception):
                PgSystemLogRepository(progress_conn).create(
                    SystemLog(
                        category=LogCategory.SCAN,
                        level=LogLevel.ERROR,
                        message="watcher_scan_failed",
                        trace_id=scan_task_id,
                        details={"error": str(exc), "traceback": traceback.format_exc()},
                    )
                )

        logger.error("watcher_scan_failed", error=str(exc))
        raise

    finally:
        if library_conn is not None:
            library_conn.close()
        if progress_conn is not None:
            progress_conn.close()
