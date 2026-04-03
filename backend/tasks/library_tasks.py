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
from backend.domain.models import ProgressTracking
from backend.services.library_scan_service import scan_directory
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def library_scan_task(root_path: str) -> str:
    """Scan a directory for audio files and persist results to the DB."""
    settings = get_settings()
    task_id = uuid.uuid4().hex
    task_started_at = datetime.now(UTC)
    last_progress: dict[str, object] = {
        "processed": 0,
        "total": 0,
        "current_path": "",
    }

    # Separate autocommit connection for progress tracking.
    # Intentional layer skip past RepositoryFactory — progress writes must be
    # visible immediately (not held in the library data transaction).
    progress_conn = None
    progress_repo: PgProgressTrackingRepository | None = None
    try:
        progress_conn = psycopg.connect(
            settings.database_url, autocommit=True, row_factory=dict_row
        )
        progress_repo = PgProgressTrackingRepository(progress_conn)

        # Initial record
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

        # Callback — closes over task_id, task_started_at, last_progress,
        # progress_repo
        def on_progress(processed: int, total: int, current_path: str) -> None:
            nonlocal last_progress
            last_progress = {
                "processed": processed,
                "total": total,
                "current_path": current_path,
            }
            assert progress_repo is not None  # for mypy — always true here
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

        files, quarantine = scan_directory(Path(root_path), on_progress=on_progress)

        # Persist library data — main transactional connection
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            repos = RepositoryFactory(conn)
            for lf in files:
                repos.library_files.upsert(lf)
            for entry in quarantine:
                repos.library_quarantine.create(entry)
            conn.commit()

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
            files_indexed=len(files),
            quarantined=len(quarantine),
        )

    except Exception as exc:
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
        if progress_conn is not None:
            progress_conn.close()

    return root_path
