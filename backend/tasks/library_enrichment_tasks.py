from __future__ import annotations

import contextlib

import structlog

from backend.config import get_settings
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, LogLevel
from backend.domain.system import SystemLog
from backend.services.library_enrichment_service import (
    enrich_by_recording,
    enrich_by_release,
)
from backend.services.mb_client import MusicBrainzApiClient
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def library_enrichment_task() -> dict[str, int]:
    """Enrich all pending library files via MusicBrainz lookups.

    Phase 1: batch by release_mbid (most efficient — one API call per release).
    Phase 2: by recording_mbid for files with no release_mbid.
    """
    settings = get_settings()
    total_enriched = 0
    total_failed = 0

    # Separate autocommit connection for system logs — survives transaction rollbacks.
    progress_conn = connect_sync(settings.database_url, autocommit=True)
    sys_log_repo = PgSystemLogRepository(progress_conn)

    try:
        with connect_sync(settings.database_url) as conn:
            repos = RepositoryFactory(conn)
            cache_repo = PgMusicBrainzCacheRepository(conn)
            mb_client = MusicBrainzApiClient(cache_repo)

            # Pre-query both phases so counts are known for enrichment_started log.
            release_rows = conn.execute("""
                SELECT DISTINCT release_mbid
                FROM library_files
                WHERE enrichment_status = 'pending'
                  AND release_mbid IS NOT NULL
            """).fetchall()
            release_mbids: list[str] = [r["release_mbid"] for r in release_rows]

            recording_rows = conn.execute("""
                SELECT DISTINCT recording_mbid
                FROM library_files
                WHERE enrichment_status = 'pending'
                  AND release_mbid IS NULL
                  AND recording_mbid IS NOT NULL
            """).fetchall()
            recording_mbids: list[str] = [r["recording_mbid"] for r in recording_rows]

            sys_log_repo.create(SystemLog(
                category=LogCategory.ENRICHMENT,
                level=LogLevel.INFO,
                message="enrichment_started",
                details={
                    "release_count": len(release_mbids),
                    "recording_count": len(recording_mbids),
                },
            ))

            for release_mbid in release_mbids:
                try:
                    count = enrich_by_release(
                        release_mbid,
                        repos.library_files,
                        repos.recordings,
                        repos.works,
                        repos.artists,
                        mb_client,
                    )
                    total_enriched += count
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    total_failed += 1
                    logger.warning(
                        "enrich_by_release_error",
                        release_mbid=release_mbid,
                        error=str(exc),
                    )

            for recording_mbid in recording_mbids:
                try:
                    count = enrich_by_recording(
                        recording_mbid,
                        repos.library_files,
                        repos.recordings,
                        repos.works,
                        repos.artists,
                        mb_client,
                    )
                    total_enriched += count
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    total_failed += 1
                    logger.warning(
                        "enrich_by_recording_error",
                        recording_mbid=recording_mbid,
                        error=str(exc),
                    )

        sys_log_repo.create(SystemLog(
            category=LogCategory.ENRICHMENT,
            level=LogLevel.INFO,
            message="enrichment_completed",
            details={"enriched": total_enriched, "failed": total_failed},
        ))

    except Exception as exc:
        with contextlib.suppress(Exception):
            sys_log_repo.create(SystemLog(
                category=LogCategory.ENRICHMENT,
                level=LogLevel.ERROR,
                message="enrichment_failed",
                details={"error": str(exc)},
            ))
        raise

    finally:
        progress_conn.close()

    logger.info(
        "library_enrichment_task_complete",
        enriched=total_enriched,
        failed=total_failed,
    )

    # Fire-and-forget: trigger MB enhancement pass
    from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

    mb_enrichment_task()

    return {"enriched": total_enriched, "failed": total_failed}
