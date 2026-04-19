from __future__ import annotations

import structlog

from backend.db.ingest_transaction import ingest_transaction
from backend.db.retry import retry_on_deadlock
from backend.services.ingestion_service import IngestionResult, ingest_csv
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@retry_on_deadlock(max_attempts=3, backoff_seconds=0.5)
def _run_ingest(
    file_bytes: bytes, file_name: str, station_id: str
) -> IngestionResult:
    """Run a single ingestion transaction, retrying on Postgres deadlocks.

    The per-station advisory lock serialises same-station uploads; the
    retry decorator covers any cross-table contention the lock doesn't.
    """
    with ingest_transaction(station_id) as repos:
        return ingest_csv(
            file_bytes=file_bytes,
            file_name=file_name,
            station_id=station_id,
            playlist_repo=repos.broadcast_playlists,
            broadcast_artist_repo=repos.broadcast_artists,
            track_identity_repo=repos.broadcast_identities,
            play_event_repo=repos.broadcast_events,
            broadcast_day_repo=repos.broadcast_days,
        )


@huey.task()  # type: ignore[untyped-decorator]
def ingestion_task(file_bytes: bytes, file_name: str, station_id: str) -> str:
    """Ingest a CSV file and enqueue the embedding task on success."""
    result = _run_ingest(file_bytes, file_name, station_id)
    logger.info("ingestion_task_complete", playlist_id=result.playlist_id)

    # Fire-and-forget: enqueue embedding task.
    # NEVER call .get() on a task from within a task (deadlocks with -w 1).
    from backend.tasks.embedding_tasks import embedding_task
    embedding_task(result.playlist_id)

    return result.playlist_id
