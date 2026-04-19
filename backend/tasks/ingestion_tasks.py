from __future__ import annotations

import time
from uuid import UUID

import psycopg
import structlog

from backend.config import get_settings
from backend.db.sync_conn import connect_sync
from backend.services.ingestion_service import IngestionResult, ingest_csv
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

# Concurrent ingestions against the same station can deadlock on the
# unique indexes in broadcast_artists / track_identities. We serialize
# same-station ingests with a transaction-scoped advisory lock and retry
# the whole task if Postgres still surfaces a deadlock.
_MAX_DEADLOCK_RETRIES = 3
_DEADLOCK_BACKOFF_SECONDS = 0.5


def _advisory_lock_key(station_id: str) -> str:
    # Stable per-station key; None/empty station falls back to a shared key so
    # station-less uploads still serialize among themselves. Canonicalize UUID
    # formatting so the same station never hashes to different keys because of
    # case or hyphen differences.
    if not station_id:
        return "ingest:no-station"
    try:
        canonical = UUID(station_id).hex
    except ValueError:
        canonical = station_id
    return f"ingest:{canonical}"


def _run_ingest_once(
    file_bytes: bytes, file_name: str, station_id: str
) -> IngestionResult:
    settings = get_settings()
    with connect_sync(settings.database_url) as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
            (_advisory_lock_key(station_id),),
        )
        repos = RepositoryFactory(conn)
        result = ingest_csv(
            file_bytes=file_bytes,
            file_name=file_name,
            station_id=station_id,
            playlist_repo=repos.broadcast_playlists,
            broadcast_artist_repo=repos.broadcast_artists,
            track_identity_repo=repos.broadcast_identities,
            play_event_repo=repos.broadcast_events,
            broadcast_day_repo=repos.broadcast_days,
        )
        conn.commit()
    return result


@huey.task()  # type: ignore[untyped-decorator]
def ingestion_task(file_bytes: bytes, file_name: str, station_id: str) -> str:
    """Ingest a CSV file and enqueue embedding task on success.

    Retries on DeadlockDetected because the whole transaction (including the
    playlist row that powers content-hash dedup) is rolled back on deadlock,
    so a fresh retry is idempotent.
    """
    last_error: psycopg.errors.DeadlockDetected | None = None
    for attempt in range(1, _MAX_DEADLOCK_RETRIES + 1):
        try:
            result = _run_ingest_once(file_bytes, file_name, station_id)
            break
        except psycopg.errors.DeadlockDetected as exc:
            last_error = exc
            logger.warning(
                "ingestion_task_deadlock_retry",
                attempt=attempt,
                max_attempts=_MAX_DEADLOCK_RETRIES,
            )
            time.sleep(_DEADLOCK_BACKOFF_SECONDS * attempt)
    else:
        assert last_error is not None
        raise last_error

    logger.info("ingestion_task_complete", playlist_id=result.playlist_id)

    # Fire-and-forget: enqueue embedding task
    # NEVER call .get() on a task from within a task (deadlocks with -w 1)
    from backend.tasks.embedding_tasks import embedding_task
    embedding_task(result.playlist_id)

    return result.playlist_id
