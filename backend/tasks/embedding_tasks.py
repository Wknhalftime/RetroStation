from __future__ import annotations

from uuid import UUID

import structlog

from backend.config import get_settings
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, TaskType
from backend.services import embedding_service
from backend.tasks._error_boundary import task_failure_telemetry
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def embedding_task(playlist_id: str) -> None:
    """Generate embeddings for unembedded artists/identities linked to this playlist.

    Wraps the pipeline stage in `task_failure_telemetry` so any exception
    (embedding model failure, repo error) surfaces as a FAILED TaskProgress
    row + ERROR SystemLog before re-raising to Huey.
    """
    settings = get_settings()
    pid = UUID(playlist_id)

    with task_failure_telemetry(TaskType.LIBRARY_ENRICHMENT, LogCategory.ENRICHMENT) as task_id:
        with connect_sync(settings.database_url) as conn:
            artist_repo = PgBroadcastArtistRepository(conn)
            identity_repo = PgBroadcastTrackIdentityRepository(conn)

            # Embed artists
            unembedded_artists = artist_repo.get_unembedded_for_playlist(pid)
            if unembedded_artists:
                texts = [a.normalized_name for a in unembedded_artists]
                vectors = embedding_service.get_embeddings(texts)
                for artist, vec in zip(unembedded_artists, vectors, strict=True):
                    artist_repo.update_embedding(artist.id, vec)
                logger.info("artists_embedded", count=len(unembedded_artists))

            # Embed identities
            unembedded_identities = identity_repo.get_unembedded_for_playlist(pid)
            if unembedded_identities:
                texts = [i.normalized_title for i in unembedded_identities]
                vectors = embedding_service.get_embeddings(texts)
                for identity, vec in zip(unembedded_identities, vectors, strict=True):
                    identity_repo.update_embedding(identity.id, vec)
                logger.info("identities_embedded", count=len(unembedded_identities))

            conn.commit()

        logger.info(
            "embedding_task_complete",
            playlist_id=playlist_id,
            task_id=task_id,
        )

    # Fire-and-forget: enqueue artist matching. Outside the telemetry
    # boundary — downstream enqueue failures are not embedding failures.
    from backend.tasks.artist_matching_tasks import artist_matching_task

    artist_matching_task(playlist_id)
