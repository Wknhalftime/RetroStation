"""
Library watcher tasks — periodic polling and targeted smart scan.

The poll task runs every 4 minutes, diffs folder hashes, and enqueues a
targeted scan for changed folders. The scan task processes changed folders
using smart per-folder diffing and chains into enrichment.
"""
from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog
from huey import crontab  # type: ignore[import-untyped]

from backend.config import get_settings
from backend.db.repositories.progress_tracking import PgTaskProgressRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import EnrichmentStatus, TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.services.folder_hash_service import coalesce_paths, diff_tree
from backend.services.grouping_service import assign_work
from backend.services.library_scan_service import scan_folder_smart
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.periodic_task(crontab(minute="*/4"))  # type: ignore[untyped-decorator]
def library_watcher_poll() -> None:
    """Poll the library directory for changes every 4 minutes."""
    settings = get_settings()

    with connect_sync(
        settings.database_url, autocommit=False,
    ) as conn:
        repos = RepositoryFactory(conn)

        root_path = repos.settings.get("local_path_prefix")
        if not root_path:
            return

        changed, pending = diff_tree(root_path, repos.library_folders)
        conn.commit()

        if not changed:
            return

        coalesced = coalesce_paths(changed)

        # Stage pending hashes with a task ID.  pending is already
        # (folder_id, new_hash) tuples from diff_tree — no extra query.
        task_id = uuid.uuid4().hex
        repos.library_folders.stage_hashes(pending, task_id)
        conn.commit()

        logger.info(
            "watcher_poll_changes_detected",
            changed=len(coalesced),
            task_id=task_id,
        )

        # Fire-and-forget: enqueue targeted scan
        library_scan_files_task(coalesced, task_id)


@huey.task()  # type: ignore[untyped-decorator]
def library_scan_files_task(
    folder_paths: list[str], task_id: str
) -> None:
    """Smart scan of specific folders, then chain into enrichment."""
    settings = get_settings()
    scan_task_id = uuid.uuid4().hex
    task_started_at = datetime.now(UTC)
    total_written = 0

    progress_conn = None
    library_conn = None

    try:
        # Autocommit connection for progress tracking
        progress_conn = connect_sync(
            settings.database_url, autocommit=True,
        )
        progress_repo = PgTaskProgressRepository(progress_conn)

        # Data connection
        library_conn = connect_sync(
            settings.database_url, autocommit=False,
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

        for idx, folder_path in enumerate(folder_paths, start=1):
            result = scan_folder_smart(
                folder_path=Path(folder_path),
                file_repo=repos.library_files,
                quarantine_repo=repos.library_quarantine,
            )
            total_written += result.files_written
            library_conn.commit()

            # Grouping pass for files in this folder without a work_id
            if result.files_written > 0:
                folder_files = repos.library_files.get_by_folder_path(
                    folder_path,
                )
                for lf in folder_files:
                    if lf.work_id is not None:
                        continue
                    try:
                        result = assign_work(
                            lf,
                            artist_repo=repos.artists,
                            work_repo=repos.works,
                            library_file_repo=repos.library_files,
                            recording_repo=repos.recordings,
                            song_master_repo=repos.song_masters,
                        )
                        if result:
                            repos.library_files.update_work_id(
                                lf.id, result.work_id,
                            )
                            if result.recording_id:
                                repos.library_files.update_recording_link(
                                    lf.id,
                                    result.recording_id,
                                    EnrichmentStatus.PENDING,
                                )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "watcher_grouping_failed",
                            file_id=str(lf.id),
                            exc_info=True,
                        )
                library_conn.commit()

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
                quarantined=result.quarantined,
            )

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

        logger.info(
            "watcher_scan_complete",
            folders=len(folder_paths),
            total_written=total_written,
        )

        # Chain into enrichment if files were written
        if total_written > 0:
            from backend.tasks.library_enrichment_tasks import (
                library_enrichment_task,
            )
            library_enrichment_task()

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

        logger.error("watcher_scan_failed", error=str(exc))
        raise

    finally:
        if library_conn is not None:
            library_conn.close()
        if progress_conn is not None:
            progress_conn.close()
