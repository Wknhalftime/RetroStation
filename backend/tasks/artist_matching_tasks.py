from __future__ import annotations

from uuid import UUID

import structlog

from backend.config import get_settings
from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.db.repositories.mapping_rules import PgMappingRuleRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, TaskType
from backend.services.artist_matching_service import match_artists_for_playlist
from backend.services.mb_client import MusicBrainzApiClient
from backend.tasks._error_boundary import task_failure_telemetry
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def artist_matching_task(playlist_id: str) -> None:
    """Run artist matching for all PENDING artists in this playlist.

    Wraps the pipeline stage in `task_failure_telemetry` so any exception
    (repo error, MB client failure, UUID parse error) is surfaced as a
    FAILED TaskProgress row + ERROR SystemLog before re-raising to Huey.
    """
    settings = get_settings()

    with task_failure_telemetry(TaskType.MATCHING, LogCategory.MATCHING) as task_id:
        with connect_sync(settings.database_url) as conn:
            match_artists_for_playlist(
                playlist_id=UUID(playlist_id),
                broadcast_artist_repo=PgBroadcastArtistRepository(conn),
                track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
                artist_repo=PgArtistRepository(conn),
                match_repo=PgMatchRepository(conn),
                rules_repo=PgMappingRuleRepository(conn),
                mb_client=MusicBrainzApiClient(PgMusicBrainzCacheRepository(conn)),
                mb_score_gap=settings.mb_score_gap,
                mb_auto_link_score=settings.mb_auto_link_score,
            )
            conn.commit()

        logger.info(
            "artist_matching_task_complete",
            playlist_id=playlist_id,
            task_id=task_id,
        )

    # Fire-and-forget: enqueue identity matching. Outside the telemetry
    # boundary — if this enqueue fails, that's a downstream concern, not an
    # artist-matching failure.
    from backend.tasks.identity_matching_tasks import identity_matching_task

    identity_matching_task(playlist_id)
