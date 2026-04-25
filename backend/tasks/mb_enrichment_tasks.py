from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
import psycopg
import structlog
from psycopg import sql

from backend.config import Settings, get_settings
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.sync_conn import connect_sync
from backend.domain.catalog import Artist, Recording, Work
from backend.domain.enums import LogCategory, LogLevel, TaskStatus, TaskType
from backend.domain.system import SystemLog, TaskProgress
from backend.services.matching_constants import MB_AUTO_LINK_SCORE
from backend.services.mb_client import MusicBrainzApiClient, MusicBrainzClientProtocol
from backend.services.mb_types import MbArtist, MbRecording
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


class _UnexpectedArtistUpdateColumnError(Exception):
    """Logic bug: a column outside the allowlist was passed to
    _apply_artist_updates. Raised instead of ValueError so the exception
    is NOT caught by _PER_ITEM_RETRIABLE_ERRORS — development-time guards
    should crash the task, not be silently recorded as per-item failures.
    """


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
        raise _UnexpectedArtistUpdateColumnError(
            f"Unexpected artist update columns: {unknown_cols!r}"
        )

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
    auto_link_score: int = MB_AUTO_LINK_SCORE,
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

    `auto_link_score` is the Tier 1 confidence threshold. Defaults to the
    module constant but the task wires `Settings.mb_auto_link_score` so
    operators can tune the threshold via env without patching code.

    Returns ArtistEnhanceOutcome so the caller increments the right counter.
    """
    if artist.mbid is None:
        results = mb_client.search_artist(artist.name)
        if not results:
            repos.artists.mark_enhanced(artist.id)
            return ArtistEnhanceOutcome.ENHANCED

        best = results[0]
        best_score = int(best.get("score", 0))
        if best_score < auto_link_score:
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
    The underlying lookup is cache-read-through — warm entries produce 0 live
    calls.

    Transient httpx failures are swallowed per-MBID: the failing MBID is
    OMITTED from the result map instead of raised. That lets the per-item
    loop in `mb_enrichment_task` fall through to its own `lookup_artist`
    call (inside the retriable-error try/except), so one flaky MB request
    during the pre-pass fails only that artist instead of aborting the whole
    task. `httpx.HTTPStatusError(404)` is NOT raised by `lookup_artist`
    (returns None), so genuine 404s still land in the map as `None`.
    """
    result: dict[str, MbArtist | None] = {}
    for mbid in set(mbids):
        try:
            result[mbid] = client.lookup_artist(mbid)
        except httpx.HTTPError as exc:
            logger.warning(
                "mb_coalesce_lookup_failed",
                mbid=mbid,
                error=str(exc),
            )
    return result


def coalesce_recording_lookups(
    mbids: Iterable[str],
    client: MusicBrainzClientProtocol,
) -> dict[str, MbRecording | None]:
    """Call lookup_recording exactly once per distinct MBID.

    Sentinel convention (mirrors coalesce_artist_lookups):
      - httpx.HTTPError -> OMIT the MBID. The per-item loop falls through
        to a live lookup_recording inside its own retriable try/except.
      - 404 (lookup_recording returns None) -> store `{mbid: None}`. The
        per-item loop treats this as authoritative "not found" and must
        NOT re-query. Distinct from key-absent.
      - success -> payload in the map.
    """
    result: dict[str, MbRecording | None] = {}
    for mbid in set(mbids):
        try:
            result[mbid] = client.lookup_recording(mbid)
        except httpx.HTTPError as exc:
            logger.warning(
                "mb_coalesce_recording_lookup_failed",
                mbid=mbid,
                error=str(exc),
            )
    return result


@dataclass(frozen=True)
class _PhaseContext:
    """Shared task/progress state passed to each phase helper.

    Frozen so a phase helper can't accidentally mutate the orchestrator's
    view of `processed_start` / `total` etc. The running `processed`
    counter is returned as a delta from each helper instead of mutated
    in place.
    """

    task_id: str
    task_started_at: datetime
    total: int
    processed_start: int
    progress_repo: PgTaskProgressRepository


@dataclass(frozen=True)
class _PhaseResult:
    """Outputs every phase produces.

    `metrics` matches the schema emitted in `mb_task_summary.phases.<name>`
    (rows_queued, distinct_mbids, live_fetches_delta, cache_hits_delta,
    duplicate_mbid_ratio). Recordings phase additionally returns
    orphans_deleted as a separate scalar — see `_run_recordings_phase`.
    """

    done: int
    failed: int
    processed_delta: int
    metrics: dict[str, object]


def _emit_progress(
    ctx: _PhaseContext,
    processed: int,
    phase: str,
    current_item: str,
) -> None:
    ctx.progress_repo.upsert(TaskProgress(
        task_id=ctx.task_id,
        task_type=TaskType.MB_ENRICHMENT,
        status=TaskStatus.RUNNING,
        progress_data={
            "processed": processed,
            "total": ctx.total,
            "current_item": current_item,
            "phase": phase,
        },
        started_at=ctx.task_started_at,
        updated_at=datetime.now(UTC),
    ))


def _phase_metrics(
    *,
    rows_queued: int,
    distinct_mbids: int,
    live_fetches_delta: int,
    cache_hits_delta: int,
) -> dict[str, object]:
    """Build the per-phase entry for the mb_task_summary event.

    `duplicate_mbid_ratio` is None only when there is no input — zero
    distinct on positive rows produces 1.0 (well-defined: "no MBID
    diversity in the input").
    """
    return {
        "rows_queued": rows_queued,
        "distinct_mbids": distinct_mbids,
        "live_fetches_delta": live_fetches_delta,
        "cache_hits_delta": cache_hits_delta,
        "duplicate_mbid_ratio": (
            1.0 - (distinct_mbids / rows_queued) if rows_queued else None
        ),
    }


def _run_artist_phase(
    pending_artists: list[Artist],
    ctx: _PhaseContext,
    settings: Settings,
) -> _PhaseResult:
    """Tier-1/2/3 artist enhancement with MBID coalescing.

    Per-row failure isolation: transient errors roll back ONE artist's
    partial writes and leave it in the queue for the next run; permanent
    404s are quarantined inside `_enhance_artist`. Whole-phase aborts
    propagate.
    """
    done = 0
    failed = 0
    processed = ctx.processed_start

    with connect_sync(settings.database_url) as conn:
        repos = RepositoryFactory(conn)
        cache_repo = PgMusicBrainzCacheRepository(conn)
        with MusicBrainzApiClient(cache_repo) as mb_client:
            live_start = mb_client.live_fetches
            hits_start = mb_client.cache_hits
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
                        artist,
                        mb_client,
                        conn,
                        repos,
                        mbid_map=mbid_map,
                        auto_link_score=settings.mb_auto_link_score,
                    )
                    # Commit applies to both ENHANCED (mark_enhanced write)
                    # and FAILED (mark_enhancement_failed write inside
                    # _enhance_artist). Both paths must persist.
                    conn.commit()
                except _PER_ITEM_RETRIABLE_ERRORS as exc:
                    # Transient per-item failure: rollback this item's
                    # partial writes and let it remain in the queue for
                    # the next run. We deliberately do NOT call
                    # mark_enhancement_failed here — that would write
                    # enhancement_error and list_unenhanced's filter
                    # would then terminally quarantine the artist
                    # despite the root cause being transient (network
                    # blip, MB 5xx, idle-connection DB error).
                    # Permanent 404s are quarantined inside _enhance_artist
                    # before this except branch is ever reached.
                    conn.rollback()
                    failed += 1
                    logger.warning(
                        "mb_artist_enhancement_failed",
                        artist_id=artist.id,
                        name=artist.name,
                        reason="per_item_exception",
                        error=str(exc),
                    )
                else:
                    if outcome is ArtistEnhanceOutcome.FAILED:
                        failed += 1
                        logger.warning(
                            "mb_artist_enhancement_failed",
                            artist_id=artist.id,
                            name=artist.name,
                            reason="mb_lookup_404",
                        )
                    else:
                        done += 1
                        logger.info(
                            "mb_artist_enhanced",
                            artist_id=artist.id,
                            name=artist.name,
                        )

                processed += 1
                _emit_progress(ctx, processed, "artists", f"artist:{artist.id}")

            logger.info(
                "mb_artist_phase_summary",
                rows_queued=rows_queued,
                artists_done=done,
                artists_failed=failed,
                cache_hits=mb_client.cache_hits,
                live_fetches=mb_client.live_fetches,
            )
            metrics = _phase_metrics(
                rows_queued=rows_queued,
                distinct_mbids=len(distinct_mbids),
                live_fetches_delta=mb_client.live_fetches - live_start,
                cache_hits_delta=mb_client.cache_hits - hits_start,
            )

    return _PhaseResult(
        done=done,
        failed=failed,
        processed_delta=processed - ctx.processed_start,
        metrics=metrics,
    )


def _run_works_phase(
    pending_works: list[Work],
    ctx: _PhaseContext,
    settings: Settings,
) -> _PhaseResult:
    """Mark queued works as enhanced. No MB calls today — only DB writes.

    `failed` counts psycopg.Error per-row failures; `done` counts
    successful mark_enhanced commits.
    """
    done = 0
    failed = 0
    processed = ctx.processed_start

    with connect_sync(settings.database_url) as conn:
        repos = RepositoryFactory(conn)

        for work in pending_works:
            # Per-item commit: a single work's failure never rolls back
            # previously successful mark_enhanced writes in this phase.
            # No MB calls / JSON parsing here, so the catch is scoped
            # to psycopg.Error — catching httpx/ValueError would mask
            # logic bugs (KeyError, AttributeError) reaching this block.
            try:
                repos.works.mark_enhanced(work.id)
                conn.commit()
            except psycopg.Error as exc:
                conn.rollback()
                failed += 1
                logger.warning(
                    "mb_work_enhancement_failed",
                    mbid=work.id,
                    title=work.title,
                    error=str(exc),
                )
            else:
                done += 1
                logger.info("mb_work_enhanced", mbid=work.id, title=work.title)

            processed += 1
            _emit_progress(ctx, processed, "works", f"work:{work.id}")
        # No phase-level commit — per-item commits already flushed each
        # successful mark_enhanced write independently.

    distinct_mbids = len({w.mbid for w in pending_works if w.mbid is not None})
    metrics = _phase_metrics(
        rows_queued=len(pending_works),
        distinct_mbids=distinct_mbids,
        # Works phase does no MB HTTP today (only mark_enhanced DB
        # writes), so live_fetches / cache_hits deltas are always
        # zero. If a future enhancement calls MB here, snapshot
        # counters around this block the same way the artist and
        # recording phases do.
        live_fetches_delta=0,
        cache_hits_delta=0,
    )

    return _PhaseResult(
        done=done,
        failed=failed,
        processed_delta=processed - ctx.processed_start,
        metrics=metrics,
    )


def _run_recordings_phase(
    pending_recordings: list[Recording],
    ctx: _PhaseContext,
    settings: Settings,
) -> tuple[_PhaseResult, int]:
    """Recording duration backfill with MBID coalescing + orphan cleanup.

    Returns `(result, orphans_deleted)`. `orphans_deleted` is the rowcount
    from the post-loop DELETE that drops recordings with no library_files
    referencing them and no enhancement attempt yet.
    """
    done = 0
    failed = 0
    processed = ctx.processed_start

    with connect_sync(settings.database_url) as conn:
        repos = RepositoryFactory(conn)
        cache_repo = PgMusicBrainzCacheRepository(conn)
        with MusicBrainzApiClient(cache_repo) as mb_client:
            live_start = mb_client.live_fetches
            hits_start = mb_client.cache_hits
            # Pre-pass: one lookup_recording per distinct MBID. Read from
            # the map inside the loop; absent keys (transient pre-pass
            # failure) fall through to a live fallback lookup — same
            # shape as the Tier 2/3 artist coalescing above.
            recording_map = coalesce_recording_lookups(
                (r.id for r in pending_recordings), mb_client,
            )
            for recording in pending_recordings:
                # Per-item commit: a single recording's failure never
                # rolls back previously successful enhancements.
                #
                # Narrow try-block: the dict-containment check and the
                # cached payload lookup are pure dict ops and cannot
                # raise; only the live lookup_recording fallback and the
                # DB writes can fail. Counter increments + success log
                # live in the `else:` branch so a logic bug there
                # surfaces instead of being silently caught.
                cache_hit = recording.id in recording_map
                cached_payload = recording_map.get(recording.id)
                try:
                    # Map hit -> use cached value (may be None for a 404
                    # that was already authoritative). Map miss -> the
                    # pre-pass failed transiently for this MBID, retry
                    # the lookup live inside this same try/except.
                    data = (
                        cached_payload
                        if cache_hit
                        else mb_client.lookup_recording(recording.id)
                    )
                    if data and recording.duration_ms is None:
                        length_ms = data.get("length")
                        if length_ms is not None:
                            conn.execute(
                                "UPDATE recordings SET duration_ms = %s WHERE id = %s",
                                (int(length_ms), recording.id),
                            )
                    repos.recordings.mark_enhanced(recording.id)
                    conn.commit()
                except _PER_ITEM_RETRIABLE_ERRORS as exc:
                    conn.rollback()
                    failed += 1
                    logger.warning(
                        "mb_recording_enhancement_failed",
                        mbid=recording.id,
                        title=recording.title,
                        error=str(exc),
                    )
                else:
                    done += 1
                    logger.info(
                        "mb_recording_enhanced",
                        mbid=recording.id,
                        title=recording.title,
                    )

                processed += 1
                _emit_progress(
                    ctx, processed, "recordings", f"recording:{recording.id}",
                )

            metrics = _phase_metrics(
                rows_queued=len(pending_recordings),
                distinct_mbids=len({r.id for r in pending_recordings}),
                live_fetches_delta=mb_client.live_fetches - live_start,
                cache_hits_delta=mb_client.cache_hits - hits_start,
            )

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
            logger.info("orphaned_recordings_cleaned", count=orphans_deleted)

        conn.commit()

    return _PhaseResult(
        done=done,
        failed=failed,
        processed_delta=processed - ctx.processed_start,
        metrics=metrics,
    ), orphans_deleted


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

    # Per-phase metrics for the final `mb_task_summary` event. Each phase
    # writes its own entry on success; on mid-task raise, partially-built
    # phases survive to the `finally` summary emission.
    phases: dict[str, dict[str, object]] = {}

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

        def _ctx(processed_start: int) -> _PhaseContext:
            return _PhaseContext(
                task_id=task_id,
                task_started_at=task_started_at,
                total=total,
                processed_start=processed_start,
                progress_repo=progress_repo,
            )

        artists_result = _run_artist_phase(pending_artists, _ctx(processed), settings)
        artists_done = artists_result.done
        artists_failed = artists_result.failed
        processed += artists_result.processed_delta
        phases["artists"] = artists_result.metrics

        works_result = _run_works_phase(pending_works, _ctx(processed), settings)
        works_done = works_result.done
        processed += works_result.processed_delta
        phases["works"] = works_result.metrics

        recordings_result, orphans_deleted = _run_recordings_phase(
            pending_recordings, _ctx(processed), settings,
        )
        recordings_done = recordings_result.done
        recordings_failed = recordings_result.failed
        processed += recordings_result.processed_delta
        phases["recordings"] = recordings_result.metrics

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
        # Emit the summary even on partial failure. `phases` is built
        # incrementally (each phase writes its own key on success), so a
        # failure mid-recordings still produces a useful "artists + works"
        # summary. Narrow suppress: only catch serialization errors from
        # an unexpected value in `phases`. A broader catch would silently
        # hide logging-infrastructure bugs we'd want to surface.
        with contextlib.suppress(TypeError, ValueError):
            logger.info(
                "mb_task_summary",
                task_type="mb_enrichment",
                phases=phases,
            )
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
