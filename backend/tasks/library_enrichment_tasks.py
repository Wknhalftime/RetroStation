from __future__ import annotations

import psycopg
import structlog
from psycopg.rows import dict_row

from backend.config import get_settings
from backend.db.repositories.mb_cache import PgMbCacheRepository
from backend.services.library_enrichment_service import (
    enrich_by_recording,
    enrich_by_release,
)
from backend.services.mb_client import RealMbClient
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

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        cache_repo = PgMbCacheRepository(conn)
        mb_client = RealMbClient(cache_repo)

        # Phase 1: distinct release_mbids from pending files
        rows = conn.execute("""
            SELECT DISTINCT release_mbid
            FROM library_files
            WHERE enrichment_status = 'pending'
              AND release_mbid IS NOT NULL
        """).fetchall()
        release_mbids: list[str] = [r["release_mbid"] for r in rows]

        for release_mbid in release_mbids:
            count = enrich_by_release(
                release_mbid,
                repos.library_files,
                repos.recordings,
                repos.works,
                repos.artists,
                mb_client,
            )
            total_enriched += count

        # Phase 2: distinct recording_mbids from still-pending files with no release_mbid
        rows2 = conn.execute("""
            SELECT DISTINCT recording_mbid
            FROM library_files
            WHERE enrichment_status = 'pending'
              AND release_mbid IS NULL
              AND recording_mbid IS NOT NULL
        """).fetchall()
        recording_mbids: list[str] = [r["recording_mbid"] for r in rows2]

        for recording_mbid in recording_mbids:
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

    logger.info(
        "library_enrichment_task_complete",
        enriched=total_enriched,
        failed=total_failed,
    )

    # Fire-and-forget: trigger MB enhancement pass (task not yet created)
    # from backend.tasks.mb_enrichment_tasks import mb_enrichment_task
    # mb_enrichment_task()

    return {"enriched": total_enriched, "failed": total_failed}
