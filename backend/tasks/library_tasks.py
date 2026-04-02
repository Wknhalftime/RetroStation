from __future__ import annotations

from pathlib import Path

import psycopg
import structlog
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.services.library_scan_service import scan_directory
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def library_scan_task(root_path: str) -> str:
    """Scan a directory for audio files and persist results to the DB."""
    settings = get_settings()

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        files, quarantine = scan_directory(Path(root_path))

        for lf in files:
            repos.library_files.upsert(lf)

        for entry in quarantine:
            repos.library_quarantine.create(entry)

        conn.commit()

    logger.info(
        "library_scan_task_complete",
        root=root_path,
        files_indexed=len(files),
        quarantined=len(quarantine),
    )

    # Fire-and-forget: enqueue enrichment task (not yet created)
    # from backend.tasks.enrichment_tasks import library_enrichment_task
    # library_enrichment_task()

    return root_path
