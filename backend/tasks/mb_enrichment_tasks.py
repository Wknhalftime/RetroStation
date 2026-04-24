from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
import psycopg
import structlog
from psycopg import sql

from backend.config import get_settings
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.sync_conn import connect_sync
from backend.domain.catalog import Artist
from backend.domain.enums import LogCategory, LogLevel, TaskStatus, TaskType
from backend.domain.system import SystemLog, TaskProgress
from backend.services.matching_constants import MB_AUTO_LINK_SCORE
from backend.services.mb_client import MusicBrainzApiClient, MusicBrainzClientProtocol
from backend.services.mb_types import MbArtist
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


class ArtistEnhanceOutcome(StrEnum):
    ENHANCED = "enhanced"
    FAILED = "failed"


_ALLOWED_ARTIST_UPDATE_COLS: frozenset[str] = frozenset(
    {"disambiguation", "sort_name", "mbid"}
)


def _apply_artist_updates(
    conn: psycopg.Connection[Any],
    artist_id: str,
    updates: dict[str, str],
) -> None:
    """Issue a targeted UPDATE for only the fields that changed.

    Column names pass through psycopg.sql.Identifier for SQL safety.
    The allowlist guard catches logic bugs at development time.
    """
    if not updates:
        return
    unknown_cols = updates.keys() - _ALLOWED_ARTIST_UPDATE_COLS
    if unknown_cols:
        raise ValueError(f"Unexpected artist update columns: {unknown_cols!r}")

    query = sql.SQL("UPDATE artists SET {sets} WHERE id = %s").format(
        sets=sql.SQL(", ").join(
            sql.SQL("{col} = %s").format(col=sql.Identifier(col))
            for col in updates
        )
    )
    # Placeholder order follows updates.keys() iteration order; Python 3.7+
    # guarantees dict insertion-order preservation so this matches the SQL.
    conn.execute(query, (*updates.values(), artist_id))


def _enhance_artist(
    artist: Artist,
    mb_client: MusicBrainzClientProtocol,
    conn: psycopg.Connection[Any],
    repos: RepositoryFactory,
    *,
    mbid_map: dict[str, MbArtist | None] | None = None,
) -> ArtistEnhanceOutcome:
    """Tiered artist enhancement.

    Tier 1 (no MBID): name search → score gate → fill fields or bail out.
    Tier 2/3 (MBID known): consult `mbid_map` (if provided) or call
    `lookup_artist` directly; fill missing disambiguation / sort_name; a
    `None` result (404 from the MB API or cached 404 in `mbid_map`) maps to
    FAILED.

    `mbid_map` is the pre-fetched batch of `lookup_artist` results keyed by
    MBID. Tier 1 ignores it; Tier 2/3 reads it so the caller can coalesce
    one live lookup per distinct MBID across the queue. A value of `None`
    in the map represents a cached 404 and is respected without re-querying.

    Returns ArtistEnhanceOutcome so the caller increments the right counter.
    """
    if artist.mbid is None:
        results = mb_client.search_artist(artist.name)
        if not results:
            repos.artists.mark_enhanced(artist.id)
            return ArtistEnhanceOutcome.ENHANCED

        best = results[0]
        best_score = int(best.get("score", 0))
        if best_score < MB_AUTO_LINK_SCORE:
            repos.artists.mark_enhanced(artist.id)
            logger.info(
                "mb_artist_no_confident_match",
                artist_id=artist.id,
                name=artist.name,
                best_score=best_score,
            )
            return ArtistEnhanceOutcome.ENHANCED

        resolved_mbid: str = best["id"]
        updates: dict[str, str] = {"mbid": resolved_mbid}
        if best.get("sort-name"):
            updates["sort_name"] = best["sort-name"]
        if best.get("disambiguation"):
            updates["disambiguation"] = best["disambiguation"]
        _apply_artist_updates(conn, artist.id, updates)
        repos.artists.mark_enhanced(artist.id)
        logger.info(
            "mb_artist_enhanced_tier1",
            artist_id=artist.id,
            name=artist.name,
            resolved_mbid=resolved_mbid,
        )
        return ArtistEnhanceOutcome.ENHANCED

    # --- Tier 2 / Tier 3: MBID known ---
    if mbid_map is not None and artist.mbid in mbid_map:
        data = mbid_map[artist.mbid]
    else:
        data = mb_client.lookup_artist(artist.mbid)

    if data is None:
        repos.artists.mark_enhancement_failed(
            artist.id,
            f"MB lookup returned 404 for mbid={artist.mbid}",
        )
        return ArtistEnhanceOutcome.FAILED

    field_updates: dict[str, str] = {}
    if not artist.disambiguation and data.get("disambiguation"):
        field_updates["disambiguation"] = data["disambiguation"]
    if artist.sort_name in ("", artist.name) and data.get("sort-name"):
        field_updates["sort_name"] = data["sort-name"]

    if field_updates:
        _apply_artist_updates(conn, artist.id, field_updates)
        logger.info(
            "mb_artist_enhanced_tier2",
            artist_id=artist.id,
            mbid=artist.mbid,
            fields_updated=list(field_updates.keys()),
        )
    else:
        logger.debug(
            "mb_artist_enhanced_tier3_no_changes",
            artist_id=artist.id,
            mbid=artist.mbid,
        )

    repos.artists.mark_enhanced(artist.id)
    return ArtistEnhanceOutcome.ENHANCED


def coalesce_artist_lookups(
    mbids: Iterable[str],
    client: MusicBrainzClientProtocol,
) -> dict[str, MbArtist | None]:
    """Call lookup_artist exactly once per distinct MBID.

    Returns {mbid: MbArtist | None}. The `set(mbids)` conversion is the dedup
    mechanism — a plain dict comprehension over a list does NOT deduplicate
    calls (it only deduplicates the output dict's keys, after each duplicate
    has already triggered a full lookup). The explicit `set()` is therefore
    required for the coalescing contract, even when callers pass an iterable
    that happens to already be a set.
    The underlying lookup is cache-read-through — warm entries produce 0 live calls.
    """
    return {mbid: client.lookup_artist(mbid) for mbid in set(mbids)}


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
                # --- Metrics counters + coalescing pre-pass ---
                rows_queued = len(pending_artists)
                distinct_mbids = {
                    a.mbid for a in pending_artists if a.mbid is not None
                }
                mbid_map = coalesce_artist_lookups(distinct_mbids, mb_client)
                # Commit pre-pass cache writes NOW so they can't be rolled
                # back by a later per-item rollback. Otherwise a queue with
                # a failing first item silently discards all coalesced
                # mb_cache rows, re-issuing live HTTP on the next run.
                conn.commit()

                logger.info(
                    "mb_artist_phase_start",
                    rows_queued=rows_queued,
                    distinct_mbids=len(distinct_mbids),
                    bare_artists=sum(
                        1 for a in pending_artists if a.mbid is None
                    ),
                )

                for artist in pending_artists:
                    try:
                        outcome = _enhance_artist(
                            artist, mb_client, conn, repos, mbid_map=mbid_map
                        )
                        # Commit applies to both ENHANCED (mark_enhanced write)
                        # and FAILED (mark_enhancement_failed write inside
                        # _enhance_artist). Both paths must persist.
                        conn.commit()
                        if outcome is ArtistEnhanceOutcome.FAILED:
                            artists_failed += 1
                            logger.warning(
                                "mb_artist_enhancement_failed",
                                mbid=artist.id,
                                name=artist.name,
                                reason="mb_lookup_404",
                                error=None,
                            )
                        else:
                            artists_done += 1
                            logger.info(
                                "mb_artist_enhanced",
                                mbid=artist.id,
                                name=artist.name,
                            )
                    except _PER_ITEM_RETRIABLE_ERRORS as exc:
                        conn.rollback()
                        error_msg = str(exc)
                        try:
                            repos.artists.mark_enhancement_failed(
                                artist.id, error_msg
                            )
                            conn.commit()
                        except psycopg.Error:
                            conn.rollback()
                            logger.warning(
                                "mb_artist_mark_failed_write_failed",
                                mbid=artist.id,
                                primary_error=error_msg,
                            )
                        artists_failed += 1
                        logger.warning(
                            "mb_artist_enhancement_failed",
                            mbid=artist.id,
                            name=artist.name,
                            reason="per_item_exception",
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

        # -------------------------------------------------------------- works
        with connect_sync(settings.database_url) as conn:
            repos = RepositoryFactory(conn)

            for work in pending_works:
                # Per-item commit: a single work's failure never rolls back
                # previously successful mark_enhanced writes in this phase.
                # This phase does only a DB UPDATE — no MB calls, no JSON
                # parsing — so the catch is scoped to psycopg.Error only.
                # httpx/ValueError can't arise here, and catching them
                # would mask logic bugs (KeyError, AttributeError) that
                # would reach this block from the mark_enhanced call path.
                try:
                    repos.works.mark_enhanced(work.id)
                    conn.commit()
                    works_done += 1
                    logger.info("mb_work_enhanced", mbid=work.id, title=work.title)
                except psycopg.Error as exc:
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
            # No phase-level commit — per-item commits already flushed each
            # successful mark_enhanced write independently.

        # --------------------------------------------------------- recordings
        with connect_sync(settings.database_url) as conn:
            repos = RepositoryFactory(conn)
            cache_repo = PgMusicBrainzCacheRepository(conn)
            with MusicBrainzApiClient(cache_repo) as mb_client:
                for recording in pending_recordings:
                    # Per-item commit: a single recording's failure never
                    # rolls back previously successful enhancements.
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
                        conn.commit()
                        recordings_done += 1
                        logger.info(
                            "mb_recording_enhanced",
                            mbid=recording.id,
                            title=recording.title,
                        )
                    except _PER_ITEM_RETRIABLE_ERRORS as exc:
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
