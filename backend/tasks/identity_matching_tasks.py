from __future__ import annotations

from uuid import UUID

import structlog

from backend.config import get_settings
from backend.db.sync_conn import connect_sync
from backend.services import master_selection_service
from backend.services.identity_matching_service import match_identities_for_playlist
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def identity_matching_task(playlist_id: str) -> None:
    """Run identity matching — terminal task in the pipeline chain."""
    settings = get_settings()

    with connect_sync(settings.database_url) as conn:
        from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
        from backend.db.repositories.library_files import PgLibraryFileRepository
        from backend.db.repositories.mapping_rules import PgMappingRuleRepository
        from backend.db.repositories.matches import PgMatchRepository
        from backend.db.repositories.recordings import PgRecordingRepository
        from backend.db.repositories.song_masters import PgSongMasterRepository
        from backend.db.repositories.track_identities import PgBroadcastTrackIdentityRepository

        work_ids = match_identities_for_playlist(
            playlist_id=UUID(playlist_id),
            track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
            broadcast_artist_repo=PgBroadcastArtistRepository(conn),
            match_repo=PgMatchRepository(conn),
            library_file_repo=PgLibraryFileRepository(conn),
            rules_repo=PgMappingRuleRepository(conn),
        )
        conn.commit()

        # Recalculate song masters for any newly matched work IDs
        if work_ids:
            master_selection_service.recalculate(
                work_ids=work_ids,
                song_master_repo=PgSongMasterRepository(conn),
                recording_repo=PgRecordingRepository(conn),
                library_file_repo=PgLibraryFileRepository(conn),
            )
            conn.commit()

    logger.info("identity_matching_task_complete", playlist_id=playlist_id)
