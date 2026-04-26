from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
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
from backend.repositories.task_progress import TaskProgressRepository
from backend.services.matching_constants import MB_AUTO_LINK_SCORE
from backend.services.mb_client import MusicBrainzApiClient, MusicBrainzClientProtocol
from backend.services.mb_types import MbArtist, MbRecording
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.huey_app import huey

# Threshold for the slow-iteration warning emitted from inside the coalesce
# helpers. If the wall-clock gap between consecutive heartbeats exceeds this,
# we log a structured warning so post-hoc analysis can spot drift toward the
# 10-minute WS stale threshold (see `backend/websocket.py:57`). Independent
# of the cadence math; this is a guardrail, not a contract.
_SLOW_ITERATION_WARN_SECONDS = 30.0

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


def _heartbeat_with_slow_warning(
    on_progress: Callable[[int, int, str], None] | None,
    last_heartbeat_at: float,
    current: int,
    total: int,
    mbid: str,
    *,
    phase_label: str,
) -> float:
    """Invoke `on_progress` and return the new last-heartbeat timestamp.

    If the gap since `last_heartbeat_at` exceeds the slow-iteration
    threshold, emit a structured warning so post-hoc analysis can spot
    cadence drift toward the WS 10-minute stale threshold. Cheap guardrail
    against future tenacity / HTTP changes silently re-introducing the
    original WS-empty bug.
    """
    if on_progress is None:
        return last_heartbeat_at
    now = time.monotonic()
    gap = now - last_heartbeat_at
    if last_heartbeat_at > 0.0 and gap > _SLOW_ITERATION_WARN_SECONDS:
        logger.warning(
            "mb_coalesce_slow_iteration",
            phase=phase_label,
            mbid=mbid,
            current=current,
            total=total,
            gap_seconds=round(gap, 2),
            threshold_seconds=_SLOW_ITERATION_WARN_SECONDS,
        )
    on_progress(current, total, mbid)
    return time.monotonic()


def coalesce_artist_lookups(
    mbids: Iterable[str],
    client: MusicBrainzClientProtocol,
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, MbArtist | None]:
    """Call lookup_artist exactly once per distinct MBID.

    Returns {mbid: MbArtist | None}. The `sorted(set(mbids))` conversion is
    the dedup mechanism — a plain dict comprehension over a list does NOT
    deduplicate calls (it only deduplicates the output dict's keys, after
    each duplicate has already triggered a full lookup). The explicit
    `set()` is therefore required for the coalescing contract, even when
    callers pass an iterable that happens to already be a set. The `sorted`
    wrapper makes iteration order deterministic so heartbeat assertions and
    the WS pre-pass stream are reproducible.
    The underlying lookup is cache-read-through — warm entries produce 0 live
    calls.

    Transient httpx failures are swallowed per-MBID: the failing MBID is
    OMITTED from the result map instead of raised. That lets the per-item
    loop in `mb_enrichment_task` fall through to its own `lookup_artist`
    call (inside the retriable-error try/except), so one flaky MB request
    during the pre-pass fails only that artist instead of aborting the whole
    task. `httpx.HTTPStatusError(404)` is NOT raised by `lookup_artist`
    (returns None), so genuine 404s still land in the map as `None`.

    `on_progress(current, total, mbid)` is invoked **at the start of each
    iteration AND in the iteration's `finally` block** — twice per MBID,
    regardless of HTTP outcome. Halves the worst-case heartbeat gap during
    sustained 429/5xx retries (see plan: "Heartbeat placement"). Callbacks
    MUST NOT raise; if one does, the helper does not swallow it.
    """
    distinct = sorted(set(mbids))
    total = len(distinct)
    result: dict[str, MbArtist | None] = {}
    last_heartbeat_at = 0.0
    for i, mbid in enumerate(distinct, start=1):
        last_heartbeat_at = _heartbeat_with_slow_warning(
            on_progress, last_heartbeat_at, i, total, mbid, phase_label="artists",
        )
        try:
            result[mbid] = client.lookup_artist(mbid)
        except httpx.HTTPError as exc:
            logger.warning(
                "mb_coalesce_lookup_failed",
                mbid=mbid,
                error=str(exc),
            )
        finally:
            last_heartbeat_at = _heartbeat_with_slow_warning(
                on_progress, last_heartbeat_at, i, total, mbid, phase_label="artists",
            )
    return result


def coalesce_recording_lookups(
    mbids: Iterable[str],
    client: MusicBrainzClientProtocol,
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, MbRecording | None]:
    """Call lookup_recording exactly once per distinct MBID.

    Sentinel convention (mirrors coalesce_artist_lookups):
      - httpx.HTTPError -> OMIT the MBID. The per-item loop falls through
        to a live lookup_recording inside its own retriable try/except.
      - 404 (lookup_recording returns None) -> store `{mbid: None}`. The
        per-item loop treats this as authoritative "not found" and must
        NOT re-query. Distinct from key-absent.
      - success -> payload in the map.

    `on_progress` and `sorted(set(mbids))` semantics match
    `coalesce_artist_lookups` — see that docstring.
    """
    distinct = sorted(set(mbids))
    total = len(distinct)
    result: dict[str, MbRecording | None] = {}
    last_heartbeat_at = 0.0
    for i, mbid in enumerate(distinct, start=1):
        last_heartbeat_at = _heartbeat_with_slow_warning(
            on_progress, last_heartbeat_at, i, total, mbid, phase_label="recordings",
        )
        try:
            result[mbid] = client.lookup_recording(mbid)
        except httpx.HTTPError as exc:
            logger.warning(
                "mb_coalesce_recording_lookup_failed",
                mbid=mbid,
                error=str(exc),
            )
        finally:
            last_heartbeat_at = _heartbeat_with_slow_warning(
                on_progress, last_heartbeat_at, i, total, mbid, phase_label="recordings",
            )
    return result


@dataclass
class _PhaseContext:
    """Shared task/progress state passed to each phase helper.

    All per-phase counters (`processed`, `done`, `failed`, `metrics`,
    `orphans_deleted`) are mutated in place by the helpers as work
    progresses, so the orchestrator's outer try/except can read accurate
    partial values if a phase raises mid-loop — otherwise the FAILED
    progress upsert and the `mb_task_summary` event would record stale
    pre-phase defaults even though per-item progress upserts inside the
    helper had already advanced the DB-visible state.

    `done` and `failed` are keyed by phase name ("artists", "works",
    "recordings") so the context is phase-agnostic; the recordings
    phase additionally writes `orphans_deleted` as a top-level scalar.
    """

    task_id: str
    task_started_at: datetime
    total: int
    # Annotated as the ABC `TaskProgressRepository`, not the concrete
    # `PgTaskProgressRepository`, so test fakes are type-compatible without
    # `# type: ignore`. Project convention (.claude/CLAUDE.md) mandates ABCs
    # for repository interfaces.
    progress_repo: TaskProgressRepository
    processed: int = 0
    done: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    metrics: dict[str, dict[str, object]] = field(default_factory=dict)
    orphans_deleted: int = 0


def _emit_pre_pass_heartbeat(
    ctx: _PhaseContext,
    phase: str,
    current: int,
    total: int,
    mbid: str,
) -> None:
    """Refresh ``updated_at`` and write a pre-pass overlay onto the existing
    ``progress_tracking`` row, without incrementing ``ctx.processed``.

    Parameter order ``(current, total, mbid)`` matches the
    ``on_progress: Callable[[int, int, str], None]`` callback convention in
    `coalesce_*_lookups`, so phase-helper closures don't have to permute
    arguments — they pass them through positionally after the fixed
    ``ctx, phase`` prefix.

    `phase` is the BASE phase string (``"artists"`` / ``"recordings"``).
    The helper appends ``-prepass`` before writing it so callers don't
    accidentally double-suffix. The overlay shape is the contract documented
    in the WS-empty-during-mb_enrichment fix plan ("Public contract — field-
    presence rules"); change ``upsert``-vs-``touch_running`` discipline only
    by reading that section.

    Errors on the heartbeat path are observability, not work accounting:
    a transient `psycopg.Error` here MUST NOT crash a pre-pass that is
    otherwise progressing. The first per-item `_advance_progress` after the
    pre-pass will surface a genuinely dead `progress_conn` (it propagates
    psycopg errors to the outer task except handler), so the swallow is
    bounded by pre-pass duration, not "forever". A `rowcount == 0` from
    `touch_running` means the row vanished (deleted externally or wrong
    task_id) — log it at WARNING level; the next `_advance_progress`
    upsert will recreate the row via INSERT ON CONFLICT (the case is
    self-healing and does not require operator intervention).
    """
    overlay: dict[str, Any] = {
        "phase": f"{phase}-prepass",
        "current_item": f"prepass:{mbid}",
        "prepass_current": current,
        "prepass_total": total,
    }
    try:
        rowcount = ctx.progress_repo.touch_running(ctx.task_id, overlay)
    except psycopg.Error as exc:
        logger.warning(
            "heartbeat_failed",
            task_id=ctx.task_id,
            phase=overlay["phase"],
            error=str(exc),
        )
        return
    if rowcount == 0:
        # WARNING, not ERROR: the row is self-healing on the next per-item
        # `_advance_progress` upsert (its INSERT ON CONFLICT path will
        # recreate the row), so this case does not require operator
        # intervention. ERROR-class would imply something an on-call needs
        # to act on; this is "unusual, worth investigating if it recurs"
        # — exactly the WARNING semantics elsewhere in this module.
        logger.warning(
            "touch_running_no_row",
            task_id=ctx.task_id,
            phase=overlay["phase"],
        )


def _advance_progress(
    ctx: _PhaseContext,
    phase: str,
    current_item: str,
) -> None:
    """Increment `ctx.processed` and emit a RUNNING progress upsert.

    Helpers call this after each row finishes (success or per-item-failure
    path); the increment must be visible to the orchestrator's outer
    except handler so the FAILED upsert reports an accurate count.
    """
    ctx.processed += 1
    ctx.progress_repo.upsert(TaskProgress(
        task_id=ctx.task_id,
        task_type=TaskType.MB_ENRICHMENT,
        status=TaskStatus.RUNNING,
        progress_data={
            "processed": ctx.processed,
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
) -> None:
    """Tier-1/2/3 artist enhancement with MBID coalescing.

    Mutates `ctx.done["artists"]`, `ctx.failed["artists"]`, and
    `ctx.metrics["artists"]` as it goes. Per-row failure isolation:
    transient errors roll back ONE artist's partial writes and leave
    it in the queue for the next run; permanent 404s are quarantined
    inside `_enhance_artist`. Whole-phase aborts propagate.
    """
    ctx.done["artists"] = 0
    ctx.failed["artists"] = 0

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

            def _artist_heartbeat(current: int, total: int, mbid: str) -> None:
                _emit_pre_pass_heartbeat(ctx, "artists", current, total, mbid)

            mbid_map = coalesce_artist_lookups(
                distinct_mbids, mb_client, on_progress=_artist_heartbeat,
            )
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
                    ctx.failed["artists"] += 1
                    logger.warning(
                        "mb_artist_enhancement_failed",
                        artist_id=artist.id,
                        name=artist.name,
                        reason="per_item_exception",
                        error=str(exc),
                    )
                else:
                    if outcome is ArtistEnhanceOutcome.FAILED:
                        ctx.failed["artists"] += 1
                        logger.warning(
                            "mb_artist_enhancement_failed",
                            artist_id=artist.id,
                            name=artist.name,
                            reason="mb_lookup_404",
                        )
                    else:
                        ctx.done["artists"] += 1
                        logger.info(
                            "mb_artist_enhanced",
                            artist_id=artist.id,
                            name=artist.name,
                        )

                _advance_progress(ctx, "artists", f"artist:{artist.id}")

            logger.info(
                "mb_artist_phase_summary",
                rows_queued=rows_queued,
                artists_done=ctx.done["artists"],
                artists_failed=ctx.failed["artists"],
                cache_hits=mb_client.cache_hits,
                live_fetches=mb_client.live_fetches,
            )
            ctx.metrics["artists"] = _phase_metrics(
                rows_queued=rows_queued,
                distinct_mbids=len(distinct_mbids),
                live_fetches_delta=mb_client.live_fetches - live_start,
                cache_hits_delta=mb_client.cache_hits - hits_start,
            )


def _run_works_phase(
    pending_works: list[Work],
    ctx: _PhaseContext,
    settings: Settings,
) -> None:
    """Mark queued works as enhanced. No MB calls today — only DB writes.

    Mutates `ctx.done["works"]`, `ctx.failed["works"]`, and
    `ctx.metrics["works"]` as it goes.
    """
    ctx.done["works"] = 0
    ctx.failed["works"] = 0

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
                ctx.failed["works"] += 1
                logger.warning(
                    "mb_work_enhancement_failed",
                    mbid=work.id,
                    title=work.title,
                    error=str(exc),
                )
            else:
                ctx.done["works"] += 1
                logger.info("mb_work_enhanced", mbid=work.id, title=work.title)

            _advance_progress(ctx, "works", f"work:{work.id}")
        # No phase-level commit — per-item commits already flushed each
        # successful mark_enhanced write independently.

    distinct_mbids = len({w.mbid for w in pending_works if w.mbid is not None})
    ctx.metrics["works"] = _phase_metrics(
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


def _run_recordings_phase(
    pending_recordings: list[Recording],
    ctx: _PhaseContext,
    settings: Settings,
) -> None:
    """Recording duration backfill with MBID coalescing + orphan cleanup.

    Mutates `ctx.done["recordings"]`, `ctx.failed["recordings"]`,
    `ctx.metrics["recordings"]`, and `ctx.orphans_deleted` (the rowcount
    from the post-loop DELETE that drops recordings with no library_files
    referencing them and no enhancement attempt yet).
    """
    ctx.done["recordings"] = 0
    ctx.failed["recordings"] = 0

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
            def _recording_heartbeat(current: int, total: int, mbid: str) -> None:
                _emit_pre_pass_heartbeat(ctx, "recordings", current, total, mbid)

            recording_map = coalesce_recording_lookups(
                (r.id for r in pending_recordings),
                mb_client,
                on_progress=_recording_heartbeat,
            )
            # Commit pre-pass cache writes NOW so they can't be rolled
            # back by a later per-item rollback. Otherwise a queue with
            # a failing first item silently discards all coalesced
            # mb_cache rows, re-issuing live HTTP on the next run.
            # Mirrors the artists phase pattern.
            conn.commit()

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
                    ctx.failed["recordings"] += 1
                    logger.warning(
                        "mb_recording_enhancement_failed",
                        mbid=recording.id,
                        title=recording.title,
                        error=str(exc),
                    )
                else:
                    ctx.done["recordings"] += 1
                    logger.info(
                        "mb_recording_enhanced",
                        mbid=recording.id,
                        title=recording.title,
                    )

                _advance_progress(ctx, "recordings", f"recording:{recording.id}")

            ctx.metrics["recordings"] = _phase_metrics(
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
        ctx.orphans_deleted = max(0, orphan_cur.rowcount)
        if ctx.orphans_deleted > 0:
            logger.info("orphaned_recordings_cleaned", count=ctx.orphans_deleted)

        conn.commit()


@huey.task()  # type: ignore[untyped-decorator]
def mb_enrichment_task() -> dict[str, int]:
    """Fill metadata on canonical entities flagged needs_enhancement=TRUE.

    Processes artists, works, and recordings in sequence, each committed
    independently.  This is the final step in the library pipeline chain.
    """
    settings = get_settings()
    task_id = uuid.uuid4().hex
    task_started_at = datetime.now(UTC)

    total = 0
    # ctx is created after `total` is computed inside the try block. The
    # outer except handler reads ctx.processed / ctx.done / ctx.failed /
    # ctx.metrics if ctx was reached, else falls back to defaults. All
    # phase counters live on ctx so a mid-loop raise leaves accurate
    # partial values visible to the FAILED progress upsert and the
    # mb_task_summary `finally` emission.
    ctx: _PhaseContext | None = None

    progress_conn: psycopg.Connection | None = None
    progress_repo: PgTaskProgressRepository | None = None
    sys_log_repo: PgSystemLogRepository | None = None

    try:
        # MUST be autocommit=True. The /ws WebSocket reader polls
        # `progress_tracking` from a separate connection on the async pool;
        # pre-pass heartbeats (`touch_running`) and per-item upserts both
        # need to be immediately visible across connections so the WS
        # broadcast doesn't go silent during long pre-pass phases. A future
        # refactor that shares a transactional connection here will
        # silently re-introduce the WS-empty-during-mb_enrichment bug. See
        # the "Public contract — autocommit dependency" section in the WS
        # fix plan for the full rationale.
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

        # Single ctx shared across phases. Helpers mutate ctx.processed /
        # ctx.done / ctx.failed / ctx.metrics per row so the outer except
        # handler can read accurate partial values if a phase raises
        # mid-loop.
        ctx = _PhaseContext(
            task_id=task_id,
            task_started_at=task_started_at,
            total=total,
            progress_repo=progress_repo,
        )

        _run_artist_phase(pending_artists, ctx, settings)
        _run_works_phase(pending_works, ctx, settings)
        _run_recordings_phase(pending_recordings, ctx, settings)

        completion_details = {
            "artists_done": ctx.done.get("artists", 0),
            "artists_failed": ctx.failed.get("artists", 0),
            "works_done": ctx.done.get("works", 0),
            "works_failed": ctx.failed.get("works", 0),
            "recordings_done": ctx.done.get("recordings", 0),
            "recordings_failed": ctx.failed.get("recordings", 0),
            "orphans_deleted": ctx.orphans_deleted,
        }

        progress_repo.upsert(TaskProgress(
            task_id=task_id,
            task_type=TaskType.MB_ENRICHMENT,
            status=TaskStatus.COMPLETED,
            progress_data={
                "processed": ctx.processed,
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
                        # Read live counter from ctx so a mid-loop raise
                        # records the actual rows processed, not the
                        # pre-phase value.
                        "processed": ctx.processed if ctx is not None else 0,
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
        # Emit the summary even on partial failure. `ctx.metrics` is built
        # incrementally (each phase writes its own key on success), so a
        # failure mid-recordings still produces a useful "artists + works"
        # summary. Narrow suppress: only catch serialization errors from
        # an unexpected value in the metrics dict. A broader catch would
        # silently hide logging-infrastructure bugs we'd want to surface.
        with contextlib.suppress(TypeError, ValueError):
            logger.info(
                "mb_task_summary",
                task_type="mb_enrichment",
                phases=ctx.metrics if ctx is not None else {},
            )
        if progress_conn is not None:
            progress_conn.close()

    artists_done = ctx.done.get("artists", 0) if ctx is not None else 0
    artists_failed = ctx.failed.get("artists", 0) if ctx is not None else 0
    works_done = ctx.done.get("works", 0) if ctx is not None else 0
    works_failed = ctx.failed.get("works", 0) if ctx is not None else 0
    recordings_done = ctx.done.get("recordings", 0) if ctx is not None else 0
    recordings_failed = ctx.failed.get("recordings", 0) if ctx is not None else 0

    logger.info(
        "mb_enrichment_task_complete",
        artists_done=artists_done,
        artists_failed=artists_failed,
        works_done=works_done,
        works_failed=works_failed,
        recordings_done=recordings_done,
        recordings_failed=recordings_failed,
    )

    return {
        "artists_done": artists_done,
        "artists_failed": artists_failed,
        "works_done": works_done,
        "works_failed": works_failed,
        "recordings_done": recordings_done,
        "recordings_failed": recordings_failed,
    }
