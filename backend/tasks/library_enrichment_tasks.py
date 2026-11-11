from __future__ import annotations

from typing import Any

import httpx
import psycopg
import structlog

from backend.config import get_settings
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, TaskType
from backend.services.library_enrichment_service import (
    EnrichmentRepos,
    enrich_by_recording,
    enrich_by_recording_batch,
    enrich_by_release,
)
from backend.services.mb_client import MusicBrainzApiClient
from backend.services.repository_factory import RepositoryFactory
from backend.services.untagged_recording_service import enrich_untagged_pair, untagged_pairs
from backend.tasks._enqueue_chain import enqueue_or_log
from backend.tasks._task_run import TaskLifecycleMessages, TaskRunConfig, task_run
from backend.tasks.huey_app import huey

# Per-item failure modes we expect from library enrichment calls:
# enrich_by_release / enrich_by_recording call mb_client (network + JSON
# decode) and write to Pg. Narrow the catch so logic bugs (KeyError,
# AttributeError, TypeError) still propagate to the task boundary.
_PER_ITEM_RETRIABLE_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    psycopg.Error,
    ValueError,
)

logger = structlog.get_logger()


# Files per batched search call and per commit. The client sends up to 100
# recording MBIDs per request, so a chunk is about one request.
_BATCH_FILES = 100

_PENDING_RELEASES_SQL = """
    SELECT DISTINCT release_mbid
    FROM library_files
    WHERE enrichment_status = 'pending'
      AND release_mbid IS NOT NULL
"""
_PENDING_RECORDINGS_SQL = """
    SELECT DISTINCT recording_mbid
    FROM library_files
    WHERE enrichment_status = 'pending'
      AND release_mbid IS NULL
      AND recording_mbid IS NOT NULL
"""


def _pending_mbids(conn: psycopg.Connection[Any], query: str, column: str) -> list[str]:
    return [row[column] for row in conn.execute(query).fetchall()]


@huey.task()  # type: ignore[untyped-decorator]
def library_enrichment_task() -> dict[str, int]:
    """Enrich all pending library files via MusicBrainz lookups.

    Pass 1: batched. Every pending file with a release and a recording MBID
            goes through one recording search per 100 files.
    Pass 2: by release_mbid, one lookup per release, for whatever pass 1
            left pending (merged MBIDs, recordings no longer on the release).
    Pass 3: by recording_mbid for files with no release_mbid.
    Pass 4: files with no MBID in their tags whose work's artist has one:
            one recording search per distinct (artist, title), a confident
            pick then goes through pass 3's lookup; the rest stay pending
            (``untagged_recording_service``).
    """
    settings = get_settings()

    total_enriched = 0
    total_failed = 0
    untagged_skipped = 0
    processed = 0
    total = 0
    task_id = ""

    config = TaskRunConfig(
        task_type=TaskType.LIBRARY_ENRICHMENT,
        log_category=LogCategory.ENRICHMENT,
        messages=TaskLifecycleMessages(
            started="enrichment_started",
            completed="enrichment_completed",
            failed="enrichment_failed",
        ),
        read_progress=lambda: (processed, total),
    )

    with task_run(settings.database_url, config) as handle:
        task_id = handle.task_id
        with connect_sync(settings.database_url) as conn:
            repos = RepositoryFactory(conn)
            cache_repo = PgMusicBrainzCacheRepository(conn)

            batch_files = repos.library_files.get_pending_enrichment_with_release()
            chunks = [
                batch_files[i : i + _BATCH_FILES] for i in range(0, len(batch_files), _BATCH_FILES)
            ]
            # Pre-query the later passes so the first progress row carries a
            # total. Pass 2 is re-queried after pass 1 shrinks it.
            release_mbids = _pending_mbids(conn, _PENDING_RELEASES_SQL, "release_mbid")
            recording_mbids = _pending_mbids(conn, _PENDING_RECORDINGS_SQL, "recording_mbid")
            # Pass 4's files carry no MBID, so passes 1-3 never change this set.
            pairs = untagged_pairs(repos.library_files.list_untagged_with_linked_artist())
            total = len(chunks) + len(release_mbids) + len(recording_mbids) + len(pairs)

            # Initial RUNNING row so the UI shows a bar even when total == 0.
            handle.report_running(
                progress_data={"processed": 0, "total": total, "current_item": ""}
            )

            handle.log_started(
                details={
                    "batch_count": len(chunks),
                    "release_count": len(release_mbids),
                    "recording_count": len(recording_mbids),
                }
            )
            # The pairs count is not in the started details: the lifecycle snapshots
            # pin those; the structured logs carry pass 4's own counts.
            logger.info("untagged_pass_planned", pairs=len(pairs))

            enrichment_repos = EnrichmentRepos(
                files=repos.library_files,
                enrichment_queries=repos.library_files,
                recordings=repos.recordings,
                works=repos.works,
                song_masters=repos.song_masters,
                matches=repos.matches,
                artists=repos.artists,
                performances=repos.performances,
            )
            with MusicBrainzApiClient(
                cache_repo,
                ttl_days=settings.mb_cache_ttl_days,
            ) as mb_client:
                for index, chunk in enumerate(chunks, start=1):
                    try:
                        outcome = enrich_by_recording_batch(
                            chunk,
                            enrichment_repos,
                            mb_client,
                        )
                        total_enriched += outcome.enriched
                        conn.commit()
                    except _PER_ITEM_RETRIABLE_ERRORS as exc:
                        # The chunk's files stay pending; pass 2 picks them up.
                        conn.rollback()
                        total_failed += 1
                        logger.warning(
                            "enrich_by_recording_batch_error",
                            batch=index,
                            files=len(chunk),
                            error=str(exc),
                        )

                    processed += 1
                    handle.report_running(
                        progress_data={
                            "processed": processed,
                            "total": total,
                            "current_item": f"batch:{index}/{len(chunks)}",
                        }
                    )

                release_mbids = _pending_mbids(conn, _PENDING_RELEASES_SQL, "release_mbid")
                total = len(chunks) + len(release_mbids) + len(recording_mbids) + len(pairs)

                for release_mbid in release_mbids:
                    try:
                        count = enrich_by_release(release_mbid, enrichment_repos, mb_client)
                        total_enriched += count
                        conn.commit()
                    except _PER_ITEM_RETRIABLE_ERRORS as exc:
                        conn.rollback()
                        total_failed += 1
                        logger.warning(
                            "enrich_by_release_error",
                            release_mbid=release_mbid,
                            error=str(exc),
                        )

                    processed += 1
                    handle.report_running(
                        progress_data={
                            "processed": processed,
                            "total": total,
                            "current_item": f"release:{release_mbid}",
                        }
                    )

                for recording_mbid in recording_mbids:
                    try:
                        count = enrich_by_recording(
                            recording_mbid,
                            enrichment_repos,
                            mb_client,
                        )
                        total_enriched += count
                        conn.commit()
                    except _PER_ITEM_RETRIABLE_ERRORS as exc:
                        conn.rollback()
                        total_failed += 1
                        logger.warning(
                            "enrich_by_recording_error",
                            recording_mbid=recording_mbid,
                            error=str(exc),
                        )

                    processed += 1
                    handle.report_running(
                        progress_data={
                            "processed": processed,
                            "total": total,
                            "current_item": f"recording:{recording_mbid}",
                        }
                    )

                for pair in pairs:
                    try:
                        pair_outcome = enrich_untagged_pair(pair, enrichment_repos, mb_client)
                        total_enriched += pair_outcome.enriched
                        untagged_skipped += len(pair_outcome.skipped)
                        conn.commit()
                    except _PER_ITEM_RETRIABLE_ERRORS as exc:
                        # The pair's files stay as they were; the next run tries again.
                        conn.rollback()
                        total_failed += 1
                        logger.warning(
                            "enrich_untagged_pair_error",
                            artist_mbid=pair.artist_mbid,
                            title=pair.title,
                            error=str(exc),
                        )

                    processed += 1
                    handle.report_running(
                        progress_data={
                            "processed": processed,
                            "total": total,
                            "current_item": f"untagged:{pair.artist_mbid}:{pair.title}",
                        }
                    )

        # COMPLETED is staged after the library connection closes so the
        # final row reflects all committed work; task_run writes it (and the
        # completed SystemLog) once this `with` block exits normally.
        handle.set_completed(
            progress_data={
                "processed": processed,
                "total": total,
                "enriched": total_enriched,
                "failed": total_failed,
            },
            details={"enriched": total_enriched, "failed": total_failed},
        )

    logger.info(
        "library_enrichment_task_complete",
        enriched=total_enriched,
        failed=total_failed,
        untagged_skipped=untagged_skipped,
    )

    # Fire-and-forget: trigger MB enhancement pass. Guarded (AUD-R012
    # (1)): this task's own run already reported COMPLETED above (by
    # `task_run` on `with`-block exit), so a downstream enqueue failure must
    # not escape unreported. `task_run`'s progress_conn is already closed at
    # this point, so a fresh short-lived autocommit connection is opened
    # just for this guarded enqueue's failure log — kept separate from (and
    # after) the envelope on purpose, so `_task_run.py`'s contract is
    # untouched and today's COMPLETED-before-enqueue order is preserved.
    from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

    with connect_sync(settings.database_url, autocommit=True) as log_conn:
        enqueue_or_log(
            mb_enrichment_task,
            task_name="mb_enrichment_task",
            caller_task_id=task_id,
            log_category=LogCategory.ENRICHMENT,
            sys_log_repo=PgSystemLogRepository(log_conn),
        )

    return {"enriched": total_enriched, "failed": total_failed}
