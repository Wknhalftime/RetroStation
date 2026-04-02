from __future__ import annotations

from uuid import UUID

import psycopg
import structlog
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.services.identity_matching_service import match_identities_for_playlist
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def identity_matching_task(playlist_id: str) -> None:
    """Run identity matching — terminal task in the pipeline chain."""
    settings = get_settings()

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        from backend.db.repositories.log_identities import PgLogIdentityRepository
        from backend.db.repositories.matches import PgMatchRepository

        # Import library_files repo — needed for future phases
        # For now, identity matching short-circuits with no library data
        from tests.fakes.library_files import FakeLibraryFileRepository

        match_identities_for_playlist(
            playlist_id=UUID(playlist_id),
            log_identity_repo=PgLogIdentityRepository(conn),
            match_repo=PgMatchRepository(conn),
            library_file_repo=FakeLibraryFileRepository(),  # empty — no library in Phase 1
        )
        conn.commit()

    logger.info("identity_matching_task_complete", playlist_id=playlist_id)
