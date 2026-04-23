from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime

import httpx
import psycopg
import structlog

from backend.config import get_settings
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, LogLevel, TaskStatus, TaskType
from backend.domain.system import SystemLog, TaskProgress
from backend.services.mb_client import MusicBrainzApiClient
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

# Per-item failure modes we expect from MB enrichment calls:
# - httpx.HTTPError: transient network failure / rate limiting / 5xx.
# - psycopg.Error: DB connectivity / constraint violation on a single write.
# - ValueError: malformed MB payload that `.get()` / `int()` converts raise on.
# Logic bugs (KeyError, AttributeError, TypeError) deliberately propagate to
# the task's outer boundary so they surface in crash logs instead of being
# silently logged per-item and continuing with a half-processed batch.
_PER_ITEM_RETRIABLE_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    psycopg.Error,
    ValueError,
)


@huey.task()  # type: ignore[untyped-decorator]
def mb_enrichment_task() -> dict[str, int]:
    """Fill metadata on canonical entities flagged needs_enhancement=TRUE.

    Processes artists, works, and recordings in sequence, each committed
    independently.  This is the final step in the library pipeline chain.
    """
    settings = get_settings()
    task_id = uuid.uuid4().hex
    task_started_at = datetime.now(UTC)

    artists_done = 0
    artists_failed = 0
    works_done = 0
    recordings_done = 0
    recordings_failed = 0
    orphans_deleted = 0
    processed = 0
    total = 0

    progress_conn: psycopg.Connection | None = None
    progress_repo: PgTaskProgressRepository | None = None
    sys_log_repo: PgSystemLogRepository | None = None

    try:
        progress_conn = connect_sync(settings.database_url, autocommit=True)
        progress_repo = PgTaskProgressRepository(progress_conn)
        sys_log_repo = PgSystemLogRepository(progress_conn)

        # Pre-count all three queues so `total` is known for the initial
        # RUNNING upsert. Each phase re-opens its own transactional connection
        # below for mutations; this counting connection is read-only.
        with connect_sync(settings.database_url) as counting_conn:
            counting_repos = RepositoryFactory(counting_conn)
            pending_artists = counting_repos.artists.list_unenhanced()
            pending_works = counting_repos.works.list_needing_enhancement()
            pending_recordings = counting_repos.recordings.list_needing_enhancement()

        total = len(pending_artists) + len(pending_works) + len(pending_recordings)

        progress_repo.upsert(TaskProgress(
            task_id=task_id,
            task_type=TaskType.MB_ENRICHMENT,
            status=TaskStatus.RUNNING,
            progress_data={
                "processed": 0,
                "total": total,
                "current_item": "",
                "phase": "artists",
            },
            started_at=task_started_at,
            updated_at=task_started_at,
        ))

        sys_log_repo.create(SystemLog(
            category=LogCategory.ENRICHMENT,
            level=LogLevel.INFO,
            message="mb_enrichment_started",
            trace_id=task_id,
            details={
                "artists": len(pending_artists),
                "works": len(pending_works),
                "recordings": len(pending_recordings),
            },
        ))

        # ------------------------------------------------------------ artists
        with connect_sync(settings.database_url) as conn:
            repos = RepositoryFactory(conn)
            cache_repo = PgMusicBrainzCacheRepository(conn)
            with MusicBrainzApiClient(cache_repo) as mb_client:
                for artist in pending_artists:
                    try:
                        results = mb_client.search_artist(artist.name)
                        match = next(
                            (r for r in results if r.get("id") == artist.id), None
                        )
                        if match and match.get("disambiguation"):
                            conn.execute(
                                "UPDATE artists SET disambiguation = %s WHERE id = %s",
                                (match["disambiguation"], artist.id),
                            )
                        repos.artists.mark_enhanced(artist.id)
                        artists_done += 1
                        logger.info("mb_artist_enhanced", mbid=artist.id, name=artist.name)
                    except _PER_ITEM_RETRIABLE_ERRORS as exc:
                        # On a psycopg.Error the connection is left in an
                        # aborted-transaction state — the mark_enhancement_failed
                        # write below (and the outer conn.commit()) would raise
                        # InFailedSqlTransaction. Roll back before any further
                        # query so per-item failure logging can continue.
                        if isinstance(exc, psycopg.Error):
                            conn.rollback()
                        error_msg = str(exc)
                        repos.artists.mark_enhancement_failed(artist.id, error_msg)
                        artists_failed += 1
                        logger.warning(
                            "mb_artist_enhancement_failed",
                            mbid=artist.id,
                            name=artist.name,
                            error=error_msg,
                        )

                    processed += 1
                    progress_repo.upsert(TaskProgress(
                        task_id=task_id,
                        task_type=TaskType.MB_ENRICHMENT,
                        status=TaskStatus.RUNNING,
                        progress_data={
                            "processed": processed,
                            "total": total,
                            "current_item": f"artist:{artist.id}",
                            "phase": "artists",
                        },
                        started_at=task_started_at,
                        updated_at=datetime.now(UTC),
                    ))

            conn.commit()

        # -------------------------------------------------------------- works
        with connect_sync(settings.database_url) as conn:
            repos = RepositoryFactory(conn)

            for work in pending_works:
                try:
                    repos.works.mark_enhanced(work.id)
                    works_done += 1
                    logger.info("mb_work_enhanced", mbid=work.id, title=work.title)
                except _PER_ITEM_RETRIABLE_ERRORS as exc:
                    # Rollback on DB errors so the outer conn.commit() at end
                    # of the works loop doesn't fail on an aborted transaction.
                    if isinstance(exc, psycopg.Error):
                        conn.rollback()
                    logger.warning(
                        "mb_work_enhancement_failed",
                        mbid=work.id,
                        title=work.title,
                        error=str(exc),
                    )

                processed += 1
                progress_repo.upsert(TaskProgress(
                    task_id=task_id,
                    task_type=TaskType.MB_ENRICHMENT,
                    status=TaskStatus.RUNNING,
                    progress_data={
                        "processed": processed,
                        "total": total,
                        "current_item": f"work:{work.id}",
                        "phase": "works",
                    },
                    started_at=task_started_at,
                    updated_at=datetime.now(UTC),
                ))

            conn.commit()

        # --------------------------------------------------------- recordings
        with connect_sync(settings.database_url) as conn:
            repos = RepositoryFactory(conn)
            cache_repo = PgMusicBrainzCacheRepository(conn)
            with MusicBrainzApiClient(cache_repo) as mb_client:
                for recording in pending_recordings:
                    try:
                        data = mb_client.lookup_recording(recording.id)
                        if data and recording.duration_ms is None:
                            length_ms = data.get("length")
                            if length_ms is not None:
                                conn.execute(
                                    "UPDATE recordings SET duration_ms = %s WHERE id = %s",
                                    (int(length_ms), recording.id),
                                )
                        repos.recordings.mark_enhanced(recording.id)
                        recordings_done += 1
                        logger.info(
                            "mb_recording_enhanced",
                            mbid=recording.id,
                            title=recording.title,
                        )
                    except _PER_ITEM_RETRIABLE_ERRORS as exc:
                        # Rollback on DB errors so the outer conn.commit() at
                        # end of the recordings loop doesn't fail on an
                        # aborted transaction.
                        if isinstance(exc, psycopg.Error):
                            conn.rollback()
                        logger.warning(
                            "mb_recording_enhancement_failed",
                            mbid=recording.id,
                            title=recording.title,
                            error=str(exc),
                        )
                        recordings_failed += 1

                    processed += 1
                    progress_repo.upsert(TaskProgress(
                        task_id=task_id,
                        task_type=TaskType.MB_ENRICHMENT,
                        status=TaskStatus.RUNNING,
                        progress_data={
                            "processed": processed,
                            "total": total,
                            "current_item": f"recording:{recording.id}",
                            "phase": "recordings",
                        },
                        started_at=task_started_at,
                        updated_at=datetime.now(UTC),
                    ))

            orphan_cur = conn.execute(
                """DELETE FROM recordings r
                   WHERE NOT EXISTS (
                       SELECT 1 FROM library_files lf
                       WHERE lf.recording_id = r.id
                   )
                   AND r.needs_enhancement = FALSE
                   AND r.enhanced_at IS NULL""",
            )
            # rowcount can be -1 (unknown) for some driver modes; clamp to 0.
            orphans_deleted = max(0, orphan_cur.rowcount)
            if orphans_deleted > 0:
                logger.info(
                    "orphaned_recordings_cleaned",
                    count=orphans_deleted,
                )

            conn.commit()

        completion_details = {
            "artists_done": artists_done,
            "artists_failed": artists_failed,
            "works_done": works_done,
            "recordings_done": recordings_done,
            "recordings_failed": recordings_failed,
            "orphans_deleted": orphans_deleted,
        }

        progress_repo.upsert(TaskProgress(
            task_id=task_id,
            task_type=TaskType.MB_ENRICHMENT,
            status=TaskStatus.COMPLETED,
            progress_data={
                "processed": processed,
                "total": total,
                **completion_details,
            },
            started_at=task_started_at,
            updated_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        ))

        sys_log_repo.create(SystemLog(
            category=LogCategory.ENRICHMENT,
            level=LogLevel.INFO,
            message="mb_enrichment_completed",
            trace_id=task_id,
            details=completion_details,
        ))

    except Exception as exc:
        if progress_repo is not None:
            with contextlib.suppress(Exception):
                progress_repo.upsert(TaskProgress(
                    task_id=task_id,
                    task_type=TaskType.MB_ENRICHMENT,
                    status=TaskStatus.FAILED,
                    progress_data={
                        "processed": processed,
                        "total": total,
                        "error": str(exc),
                    },
                    started_at=task_started_at,
                    updated_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                ))
        if sys_log_repo is not None:
            with contextlib.suppress(Exception):
                sys_log_repo.create(SystemLog(
                    category=LogCategory.ENRICHMENT,
                    level=LogLevel.ERROR,
                    message="mb_enrichment_failed",
                    trace_id=task_id,
                    details={"error": str(exc)},
                ))
        raise

    finally:
        if progress_conn is not None:
            progress_conn.close()

    logger.info(
        "mb_enrichment_task_complete",
        artists_done=artists_done,
        artists_failed=artists_failed,
        works_done=works_done,
        recordings_done=recordings_done,
        recordings_failed=recordings_failed,
    )

    return {
        "artists_done": artists_done,
        "artists_failed": artists_failed,
        "works_done": works_done,
        "recordings_done": recordings_done,
        "recordings_failed": recordings_failed,
    }
