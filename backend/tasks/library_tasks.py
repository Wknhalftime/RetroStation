from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import structlog
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.db.repositories.progress_tracking import PgProgressTrackingRepository
from backend.domain.enums import TaskStatus, TaskType
from backend.domain.models import LibraryFile, LibraryQuarantine, ProgressTracking
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
    progress_repo: PgProgressTrackingRepository,
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
    quarantine_written = 0
    pending_writes = 0

    # --- Callbacks ---

    def on_file(lf: LibraryFile) -> None:
        nonlocal pending_writes, files_written
        repos.library_files.upsert_write_only(lf)
        files_written += 1
        pending_writes += 1
        if pending_writes >= chunk_size:
            library_conn.commit()
            pending_writes = 0

    def on_quarantine(entry: LibraryQuarantine) -> None:
        nonlocal pending_writes, quarantine_written
        repos.library_quarantine.create_write_only(entry)
        quarantine_written += 1
        pending_writes += 1
        if pending_writes >= chunk_size:
            library_conn.commit()
            pending_writes = 0

    def on_progress(processed: int, total: int, current_path: str) -> None:
        nonlocal last_progress
        last_progress = {
            "processed": processed,
            "total": total,
            "current_path": current_path,
        }
        progress_repo.upsert(
            ProgressTracking(
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

    # Commit any remaining writes from the last partial chunk
    if pending_writes > 0:
        library_conn.commit()

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
    progress_repo: PgProgressTrackingRepository | None = None
    library_conn = None

    try:
        # Autocommit connection for progress tracking
        progress_conn = psycopg.connect(
            settings.database_url, autocommit=True, row_factory=dict_row
        )
        progress_repo = PgProgressTrackingRepository(progress_conn)

        # Initial progress record
        progress_repo.upsert(
            ProgressTracking(
                task_id=task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.RUNNING,
                progress_data=last_progress,
                started_at=task_started_at,
                updated_at=task_started_at,
            )
        )

        # Open library connection BEFORE scan so callbacks can write immediately
        library_conn = psycopg.connect(
            settings.database_url, autocommit=False, row_factory=dict_row
        )
        repos = RepositoryFactory(library_conn)

        files_written, quarantine_written, last_progress = _run_scan(
            root_path=root_path,
            library_conn=library_conn,
            repos=repos,
            progress_repo=progress_repo,
            task_id=task_id,
        )

        # Mark completed AFTER library data commit succeeds
        progress_repo.upsert(
            ProgressTracking(
                task_id=task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.COMPLETED,
                progress_data=last_progress,
                started_at=task_started_at,
                updated_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )

        logger.info(
            "library_scan_task_complete",
            root=root_path,
            files_indexed=files_written,
            quarantined=quarantine_written,
        )

    except Exception as exc:
        if library_conn is not None:
            with contextlib.suppress(Exception):
                library_conn.rollback()

        if progress_conn is not None and progress_repo is not None:
            with contextlib.suppress(Exception):
                progress_repo.upsert(
                    ProgressTracking(
                        task_id=task_id,
                        task_type=TaskType.SCAN,
                        status=TaskStatus.FAILED,
                        progress_data={**last_progress, "error": str(exc)},
                        started_at=task_started_at,
                        updated_at=datetime.now(UTC),
                        completed_at=datetime.now(UTC),
                    )
                )
        raise

    finally:
        if library_conn is not None:
            library_conn.close()
        if progress_conn is not None:
            progress_conn.close()

    return root_path
