from __future__ import annotations

import structlog

from backend.config import get_settings
from backend.db.sync_conn import connect_sync
from backend.services.ingestion_service import ingest_csv
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def ingestion_task(file_bytes: bytes, file_name: str, station_id: str) -> str:
    """Ingest a CSV file and enqueue embedding task on success."""
    settings = get_settings()

    with connect_sync(settings.database_url) as conn:
        repos = RepositoryFactory(conn)
        result = ingest_csv(
            file_bytes=file_bytes,
            file_name=file_name,
            station_id=station_id,
            playlist_repo=repos.playlists,
            broadcast_artist_repo=repos.broadcast_artists,
            track_identity_repo=repos.track_identities,
            play_event_repo=repos.play_events,
            broadcast_day_repo=repos.broadcast_days,
        )
        conn.commit()

    logger.info("ingestion_task_complete", playlist_id=result.playlist_id)

    # Fire-and-forget: enqueue embedding task
    # NEVER call .get() on a task from within a task (deadlocks with -w 1)
    from backend.tasks.embedding_tasks import embedding_task
    embedding_task(result.playlist_id)

    return result.playlist_id
