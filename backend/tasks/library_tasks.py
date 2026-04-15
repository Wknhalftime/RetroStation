from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import structlog

from backend.config import get_settings
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import (
    EnrichmentStatus,
    LogCategory,
    LogLevel,
    TaskStatus,
    TaskType,
)
from backend.domain.library import LibraryFile, LibraryQuarantine
from backend.domain.system import SystemLog, TaskProgress
from backend.services.folder_hash_service import diff_tree
from backend.services.grouping_service import assign_work
from backend.services.library_scan_service import scan_directory
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

COMMIT_CHUNK_SIZE = 100


def _run_scan(
    *,
    root_path: str,
    library_conn: psycopg.Connection,
    repos: RepositoryFactory,
    progress_repo: PgTaskProgressRepository,
    task_id: str,
    chunk_size: int = COMMIT_CHUNK_SIZE,
) -> tuple[int, int, dict[str, object]]:
    """Core scan logic — extracted from the Huey task so it is directly testable.

    Opens no connections itself; callers provide them.
    Returns ``(files_written, quarantine_written, last_progress)``.
    """
    task_started_at = datetime.now(UTC)
    last_progress: dict[str, object] = {
        "processed": 0,
        "total": 0,
        "current_path": "",
    }

    files_written = 0
    files_committed = 0
    quarantine_written = 0
    pending_writes = 0
    written_files: list[LibraryFile] = []

    # --- Callbacks ---

    def _flush_chunk() -> None:
        nonlocal pending_writes, files_committed
        library_conn.commit()
        files_committed += pending_writes
        pending_writes = 0

    def on_file(lf: LibraryFile) -> None:
        nonlocal pending_writes, files_written
        try:
            repos.library_files.upsert_write_only(lf)
        except psycopg.Error:
            # Bad metadata (e.g. null bytes) can poison the transaction.
            # Rollback discards uncommitted chunk rows — they will be
            # re-inserted on the next scan since the upsert is idempotent.
            library_conn.rollback()
            logger.warning(
                "file_insert_failed_quarantined",
                file_path=lf.file_path, exc_info=True,
            )
            repos.library_quarantine.create_write_only(
                LibraryQuarantine(
                    id=lf.id,
                    file_path=lf.file_path,
                    error_message="metadata contains invalid characters",
                )
            )
            pending_writes = 1  # quarantine row is only uncommitted write
            return
        files_written += 1
        pending_writes += 1
        written_files.append(lf)
        if pending_writes >= chunk_size:
            _flush_chunk()

    def on_quarantine(entry: LibraryQuarantine) -> None:
        nonlocal pending_writes, quarantine_written
        repos.library_quarantine.create_write_only(entry)
        quarantine_written += 1
        pending_writes += 1
        if pending_writes >= chunk_size:
            _flush_chunk()

    def on_progress(processed: int, total: int, current_path: str) -> None:
        nonlocal last_progress
        last_progress = {
            "processed": processed,
            "total": total,
            "current_path": current_path,
            "files_committed": files_committed,
        }
        progress_repo.upsert(
            TaskProgress(
                task_id=task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.RUNNING,
                progress_data=last_progress,
                started_at=task_started_at,
                updated_at=datetime.now(UTC),
            )
        )

    # --- Run scan with callbacks ---
    scan_directory(
        Path(root_path),
        on_progress=on_progress,
        on_file=on_file,
        on_quarantine=on_quarantine,
    )

    # Flush any remaining scan writes before folder-tree pass
    if pending_writes > 0:
        _flush_chunk()

    # Build folder tree so library_folders is populated even on first scan
    diff_tree(root_path, repos.library_folders)

    # Commit unconditionally: diff_tree always writes folder rows to library_conn
    # regardless of whether any audio files were scanned, so we must not guard
    # this commit behind pending_writes > 0.
    library_conn.commit()

    # --- Grouping pass: assign work_id to newly written files ---
    grouped = 0
    grouping_pending = 0
    for lf in written_files:
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
                repos.library_files.update_work_id(lf.id, result.work_id)
                if result.recording_id:
                    repos.library_files.update_recording_link(
                        lf.id,
                        result.recording_id,
                        EnrichmentStatus.PENDING,
                    )
                grouped += 1
                grouping_pending += 1
                if grouping_pending >= chunk_size:
                    library_conn.commit()
                    grouping_pending = 0
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "grouping_failed",
                file_id=str(lf.id),
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            if isinstance(exc, psycopg.Error):
                library_conn.rollback()
                grouping_pending = 0
    if grouping_pending > 0:
        library_conn.commit()
    if grouped > 0:
        logger.info("scan_grouping_complete", grouped=grouped, total=len(written_files))

    return files_written, quarantine_written, last_progress


@huey.task()  # type: ignore[untyped-decorator]
def library_scan_task(root_path: str) -> str:
    """Scan a directory for audio files and persist results to the DB."""
    logger.info("library_scan_task_started", root=root_path)
    settings = get_settings()
    task_id = uuid.uuid4().hex
    task_started_at = datetime.now(UTC)
    last_progress: dict[str, object] = {
        "processed": 0,
        "total": 0,
        "current_path": "",
    }

    progress_conn = None
    progress_repo: PgTaskProgressRepository | None = None
    sys_log_repo: PgSystemLogRepository | None = None
    library_conn = None

    try:
        # Autocommit connection for progress tracking and system logs
        progress_conn = connect_sync(
            settings.database_url, autocommit=True,
        )
        progress_repo = PgTaskProgressRepository(progress_conn)
        sys_log_repo = PgSystemLogRepository(progress_conn)

        # Initial progress record
        progress_repo.upsert(
            TaskProgress(
                task_id=task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.RUNNING,
                progress_data=last_progress,
                started_at=task_started_at,
                updated_at=task_started_at,
            )
        )

        sys_log_repo.create(SystemLog(
            category=LogCategory.SCAN,
            level=LogLevel.INFO,
            message="scan_started",
            trace_id=task_id,
            details={"root": root_path},
        ))

        # Open library connection BEFORE scan so callbacks can write immediately
        library_conn = connect_sync(
            settings.database_url, autocommit=False,
        )
        repos = RepositoryFactory(library_conn)

        files_written, quarantine_written, last_progress = _run_scan(
            root_path=root_path,
            library_conn=library_conn,
            repos=repos,
            progress_repo=progress_repo,
            task_id=task_id,
        )

        if files_written == 0:
            last_progress["warning"] = "no_audio_files_found"
            logger.warning("library_scan_zero_files", root=root_path)

        # Mark completed AFTER library data commit succeeds
        progress_repo.upsert(
            TaskProgress(
                task_id=task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.COMPLETED,
                progress_data=last_progress,
                started_at=task_started_at,
                updated_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )

        sys_log_repo.create(SystemLog(
            category=LogCategory.SCAN,
            level=LogLevel.INFO,
            message="scan_completed",
            trace_id=task_id,
            details={"files_indexed": files_written, "quarantined": quarantine_written},
        ))

        logger.info(
            "library_scan_task_complete",
            root=root_path,
            files_indexed=files_written,
            quarantined=quarantine_written,
        )

        # Fire-and-forget: chain into enrichment if any files were written
        if files_written > 0:
            from backend.tasks.library_enrichment_tasks import library_enrichment_task

            library_enrichment_task()

    except Exception as exc:
        if library_conn is not None:
            with contextlib.suppress(Exception):
                library_conn.rollback()

        if progress_conn is not None and progress_repo is not None:
            with contextlib.suppress(Exception):
                progress_repo.upsert(
                    TaskProgress(
                        task_id=task_id,
                        task_type=TaskType.SCAN,
                        status=TaskStatus.FAILED,
                        progress_data={**last_progress, "error": str(exc)},
                        started_at=task_started_at,
                        updated_at=datetime.now(UTC),
                        completed_at=datetime.now(UTC),
                    )
                )
        if progress_conn is not None and sys_log_repo is not None:
            with contextlib.suppress(Exception):
                sys_log_repo.create(SystemLog(
                    category=LogCategory.SCAN,
                    level=LogLevel.ERROR,
                    message="scan_failed",
                    trace_id=task_id,
                    details={"error": str(exc)},
                ))
        raise

    finally:
        if library_conn is not None:
            library_conn.close()
        if progress_conn is not None:
            progress_conn.close()

    return root_path
