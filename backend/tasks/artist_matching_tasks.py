from __future__ import annotations

import structlog

from backend.config import get_settings
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.db.sync_conn import connect_sync
from backend.services.artist_matching_service import match_artists_for_playlist
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def artist_matching_task(playlist_id: str) -> None:
    """Run artist matching for all PENDING artists in this playlist."""
    settings = get_settings()
    from uuid import UUID

    with connect_sync(settings.database_url) as conn:
        from backend.db.repositories.artists import (
            PgArtistRepository,
        )
        from backend.db.repositories.mapping_rules import (
            PgMappingRuleRepository,
        )
        from backend.db.repositories.matches import (
            PgMatchRepository,
        )
        from backend.db.repositories.musicbrainz_cache import (
            PgMusicBrainzCacheRepository,
        )
        from backend.services.mb_client import RealMbClient

        match_artists_for_playlist(
            playlist_id=UUID(playlist_id),
            broadcast_artist_repo=PgBroadcastArtistRepository(conn),
            track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
            artist_repo=PgArtistRepository(conn),
            match_repo=PgMatchRepository(conn),
            rules_repo=PgMappingRuleRepository(conn),
            mb_client=RealMbClient(PgMusicBrainzCacheRepository(conn)),
            mb_auto_link_score=settings.mb_auto_link_score,
            mb_score_gap=settings.mb_score_gap,
        )
        conn.commit()

    logger.info("artist_matching_task_complete", playlist_id=playlist_id)

    # Fire-and-forget: enqueue identity matching
    from backend.tasks.identity_matching_tasks import (
        identity_matching_task,
    )

    identity_matching_task(playlist_id)
