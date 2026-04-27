from __future__ import annotations

from uuid import UUID

import structlog

from backend.config import get_settings
from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_track_identities import (
    PgBroadcastTrackIdentityRepository,
)
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.mapping_rules import PgMappingRuleRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.song_masters import PgSongMasterRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, TaskType
from backend.services import master_selection_service
from backend.services.identity_matching_service import match_identities_for_playlist
from backend.services.mb_client import MusicBrainzApiClient
from backend.tasks._error_boundary import task_failure_telemetry
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def identity_matching_task(playlist_id: str) -> None:
    """Run identity matching — terminal task in the pipeline chain.

    Wraps the pipeline stage in `task_failure_telemetry` so any exception
    surfaces as a FAILED TaskProgress row + ERROR SystemLog before
    re-raising to Huey.
    """
    settings = get_settings()

    with task_failure_telemetry(TaskType.MATCHING, LogCategory.MATCHING) as task_id:
        with connect_sync(settings.database_url) as conn:
            with MusicBrainzApiClient(PgMusicBrainzCacheRepository(conn)) as mb_client:
                work_ids = match_identities_for_playlist(
                    playlist_id=UUID(playlist_id),
                    track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
                    broadcast_artist_repo=PgBroadcastArtistRepository(conn),
                    match_repo=PgMatchRepository(conn),
                    library_file_repo=PgLibraryFileRepository(conn),
                    rules_repo=PgMappingRuleRepository(conn),
                    mb_client=mb_client,
                    catalog_repo=PgArtistRepository(conn),
                    strong_match_threshold=settings.strong_match_threshold,
                )
            conn.commit()

            # Recalculate song masters for any newly matched work IDs
            if work_ids:
                master_selection_service.recalculate_song_masters(
                    work_ids=work_ids,
                    song_master_repo=PgSongMasterRepository(conn),
                    recording_repo=PgRecordingRepository(conn),
                    library_file_repo=PgLibraryFileRepository(conn),
                )
                conn.commit()

        logger.info(
            "identity_matching_task_complete",
            playlist_id=playlist_id,
            task_id=task_id,
        )
