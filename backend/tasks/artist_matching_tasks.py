from __future__ import annotations

import psycopg
import structlog
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.db.repositories.log_artists import PgLogArtistRepository
from backend.db.repositories.log_identities import PgLogIdentityRepository
from backend.services.artist_matching_service import match_artists_for_playlist
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def artist_matching_task(playlist_id: str) -> None:
    """Run artist matching for all PENDING artists in this playlist."""
    settings = get_settings()
    from uuid import UUID

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        from backend.db.repositories.artists import (  # type: ignore[import-untyped]
            PgArtistRepository,
        )
        from backend.db.repositories.global_mapping_rules import (  # type: ignore[import-untyped]
            PgGlobalMappingRuleRepository,
        )
        from backend.db.repositories.matches import (  # type: ignore[import-untyped]
            PgMatchRepository,
        )
        from backend.db.repositories.mb_cache import (  # type: ignore[import-untyped]
            PgMbCacheRepository,
        )
        from backend.services.mb_client import RealMbClient  # type: ignore[import-untyped]

        match_artists_for_playlist(
            playlist_id=UUID(playlist_id),
            log_artist_repo=PgLogArtistRepository(conn),
            log_identity_repo=PgLogIdentityRepository(conn),
            artist_repo=PgArtistRepository(conn),
            match_repo=PgMatchRepository(conn),
            rules_repo=PgGlobalMappingRuleRepository(conn),
            mb_client=RealMbClient(PgMbCacheRepository(conn)),
            mb_auto_link_score=settings.mb_auto_link_score,
            mb_score_gap=settings.mb_score_gap,
        )
        conn.commit()

    logger.info("artist_matching_task_complete", playlist_id=playlist_id)

    # Fire-and-forget: enqueue identity matching
    from backend.tasks.identity_matching_tasks import (  # type: ignore[import-untyped]
        identity_matching_task,
    )

    identity_matching_task(playlist_id)
