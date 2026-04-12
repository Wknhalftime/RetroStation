from __future__ import annotations

import structlog

from backend.config import get_settings
from backend.db.repositories.mb_cache import PgMusicBrainzCacheRepository
from backend.db.sync_conn import connect_sync
from backend.services.mb_client import RealMbClient
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


@huey.task()  # type: ignore[untyped-decorator]
def mb_enrichment_task() -> dict[str, int]:
    """Fill metadata on canonical entities flagged needs_enhancement=TRUE.

    Processes artists, works, and recordings in sequence, each committed
    independently.  This is the final step in the library pipeline chain.
    """
    settings = get_settings()
    artists_done = 0
    artists_failed = 0
    works_done = 0
    recordings_done = 0
    recordings_failed = 0

    # ------------------------------------------------------------------ artists
    with connect_sync(settings.database_url) as conn:
        repos = RepositoryFactory(conn)
        cache_repo = PgMusicBrainzCacheRepository(conn)
        mb_client = RealMbClient(cache_repo)

        pending_artists = repos.artists.fetch_unenhanced()
        for artist in pending_artists:
            try:
                results = mb_client.search_artist(artist.name)
                # Find the exact MBID match in the result set
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
            except Exception as exc:  # noqa: BLE001
                error_msg = str(exc)
                repos.artists.mark_enhancement_failed(artist.id, error_msg)
                artists_failed += 1
                logger.warning(
                    "mb_artist_enhancement_failed",
                    mbid=artist.id,
                    name=artist.name,
                    error=error_msg,
                )

        conn.commit()

    # ------------------------------------------------------------------ works
    with connect_sync(settings.database_url) as conn:
        repos = RepositoryFactory(conn)

        pending_works = repos.works.list_needing_enhancement()
        for work in pending_works:
            try:
                # Works are already fully populated during enrichment; just mark done.
                repos.works.mark_enhanced(work.id)
                works_done += 1
                logger.info("mb_work_enhanced", mbid=work.id, title=work.title)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "mb_work_enhancement_failed",
                    mbid=work.id,
                    title=work.title,
                    error=str(exc),
                )

        conn.commit()

    # --------------------------------------------------------------- recordings
    with connect_sync(settings.database_url) as conn:
        repos = RepositoryFactory(conn)
        cache_repo = PgMusicBrainzCacheRepository(conn)
        mb_client = RealMbClient(cache_repo)

        pending_recordings = repos.recordings.list_needing_enhancement()
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
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "mb_recording_enhancement_failed",
                    mbid=recording.id,
                    title=recording.title,
                    error=str(exc),
                )
                recordings_failed += 1

        # Clean up orphaned local recordings (not referenced by any file,
        # created by grouping but superseded by MB enrichment).
        orphan_cur = conn.execute(
            """DELETE FROM recordings r
               WHERE NOT EXISTS (
                   SELECT 1 FROM library_files lf
                   WHERE lf.recording_id = r.id
               )
               AND r.needs_enhancement = FALSE
               AND r.enhanced_at IS NULL""",
        )
        orphans_deleted = (
            orphan_cur.rowcount if orphan_cur.rowcount else 0
        )
        if orphans_deleted > 0:
            logger.info(
                "orphaned_recordings_cleaned",
                count=orphans_deleted,
            )

        conn.commit()

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
