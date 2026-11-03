from __future__ import annotations

import contextlib
import time
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
from backend.db.sync_conn import connect_sync
from backend.domain.catalog import Artist, Recording, Work
from backend.domain.enums import LogCategory, RecheckScope, TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.repositories.task_progress import TaskProgressRepository
from backend.services.library_enrichment_service import (
    EnrichmentRepos,
    apply_recording_lookup,
    needs_recording_lookup,
)
from backend.services.mb_client import (
    MusicBrainzApiClient,
    MusicBrainzClientProtocol,
    Progress,
)
from backend.services.mb_types import MbArtist, MbRecording
from backend.services.repository_factory import RepositoryFactory
from backend.tasks._enqueue_chain import enqueue_or_log
from backend.tasks._task_run import TaskLifecycleMessages, TaskRunConfig, task_run
from backend.tasks.huey_app import huey

# Threshold for the slow-iteration warning emitted from the phases' heartbeat
# callbacks. If the wall-clock gap between consecutive heartbeats exceeds this,
# we log a structured warning so post-hoc analysis can spot drift toward the
# 10-minute WS stale threshold (`STALE_THRESHOLD_MINUTES` in `backend/websocket.py`). Independent
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


_ALLOWED_ARTIST_UPDATE_COLS: frozenset[str] = frozenset({"disambiguation", "sort_name", "mbid"})


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
            sql.SQL("{col} = %s").format(col=sql.Identifier(col)) for col in updates
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
    mbid_map: dict[str, MbArtist] | None = None,
) -> ArtistEnhanceOutcome:
    """Fill in missing artist fields from a MusicBrainz artist lookup.

    Artists gain an MBID through release / recording enrichment
    (`ArtistRepository.upsert_musicbrainz_artist`) or the local-artist linker
    (`link_local_artists_task`, AUD-R026), never here (AUD-R008).
    `upsert_local_artist` inserts `needs_enhancement=FALSE`, so a bare local
    artist should never reach this function; if one does, it is a logic
    bug, not a work item, and is quarantined below instead of being
    retried or crashing the phase.

    Consults `mbid_map` (if provided) or calls `lookup_artist` directly;
    fills missing disambiguation / sort_name; a `None` result (404 from the
    MB API or a cached 404 in `mbid_map`) maps to FAILED.

    `mbid_map` is the pre-fetched batch from `search_artists_by_mbids`, keyed
    by MBID and found-only: an MBID absent from it (merged or deleted) falls
    through to a live `lookup_artist`, which follows merges. When the whole
    batch fails, the phase passes an empty map, so every artist takes that
    live lookup inside its own per-item retriable try/except.

    Returns ArtistEnhanceOutcome so the caller increments the right counter.
    """
    if artist.mbid is None:
        logger.warning(
            "mb_artist_missing_mbid",
            artist_id=artist.id,
            name=artist.name,
        )
        repos.artists.mark_enhancement_failed(
            artist.id,
            "no MBID: artists gain MBIDs only via release enrichment",
        )
        return ArtistEnhanceOutcome.FAILED

    data: MbArtist | None
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
    on_progress: Progress | None,
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
    # `# type: ignore`. Repository interfaces are ABC ports in
    # backend/repositories (ARCHITECTURE.md, enforced by import-linter).
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
    ``Progress`` callback convention of the batch lookups in
    `MusicBrainzClientProtocol`, so phase-helper closures don't have to permute
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
    ctx.progress_repo.upsert(
        TaskProgress(
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
        )
    )


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
        "duplicate_mbid_ratio": (1.0 - (distinct_mbids / rows_queued) if rows_queued else None),
    }


def _run_artist_phase(
    pending_artists: list[Artist],
    ctx: _PhaseContext,
    settings: Settings,
) -> None:
    """Artist MB-lookup enhancement (MBID known) with MBID coalescing.

    Mutates `ctx.done["artists"]`, `ctx.failed["artists"]`, and
    `ctx.metrics["artists"]` as it goes. Per-row failure isolation:
    transient errors roll back ONE artist's partial writes and leave
    it in the queue for the next run; permanent 404s and MBID-less rows
    (a logic bug per AUD-R008) are quarantined inside `_enhance_artist`.
    Whole-phase aborts propagate.
    """
    ctx.done["artists"] = 0
    ctx.failed["artists"] = 0

    with connect_sync(settings.database_url) as conn:
        repos = RepositoryFactory(conn)
        cache_repo = PgMusicBrainzCacheRepository(conn)
        with MusicBrainzApiClient(cache_repo, ttl_days=settings.mb_cache_ttl_days) as mb_client:
            live_start = mb_client.live_fetches
            hits_start = mb_client.cache_hits
            rows_queued = len(pending_artists)
            distinct_mbids = {a.mbid for a in pending_artists if a.mbid is not None}

            last_heartbeat_at = 0.0

            def _artist_heartbeat(current: int, total: int, mbid: str) -> None:
                nonlocal last_heartbeat_at
                last_heartbeat_at = _heartbeat_with_slow_warning(
                    lambda c, t, m: _emit_pre_pass_heartbeat(ctx, "artists", c, t, m),
                    last_heartbeat_at,
                    current,
                    total,
                    mbid,
                    phase_label="artists",
                )

            try:
                mbid_map = mb_client.search_artists_by_mbids(
                    sorted(distinct_mbids), on_progress=_artist_heartbeat
                )
            except httpx.HTTPError as exc:
                # One failed batch must not fail the task: an empty map sends every
                # artist to the live lookup in the per-item loop below.
                logger.warning(
                    "mb_artist_batch_failed",
                    error=str(exc),
                    distinct=len(distinct_mbids),
                )
                mbid_map = {}
            # Commit pre-pass cache writes NOW so they can't be rolled
            # back by a later per-item rollback. Otherwise a queue with
            # a failing first item silently discards all coalesced
            # mb_cache rows, re-issuing live HTTP on the next run.
            conn.commit()

            logger.info(
                "mb_artist_phase_start",
                rows_queued=rows_queued,
                distinct_mbids=len(distinct_mbids),
            )

            for artist in pending_artists:
                try:
                    outcome = _enhance_artist(
                        artist,
                        mb_client,
                        conn,
                        repos,
                        mbid_map=mbid_map,
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
    """Recording lookup phase with MBID coalescing + orphan cleanup.

    A recording that lacks a duration or a MusicBrainz work is looked up
    (`needs_recording_lookup`) and the payload's length, live flag and
    performed work are applied (`apply_recording_lookup`); the rest are
    marked enhanced without a call.

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
        with MusicBrainzApiClient(cache_repo, ttl_days=settings.mb_cache_ttl_days) as mb_client:
            live_start = mb_client.live_fetches
            hits_start = mb_client.cache_hits

            # Pre-pass: one lookup_recording per distinct MBID. Read from
            # the map inside the loop; absent keys (transient pre-pass
            # failure) fall through to a live fallback lookup — same
            # shape as the Tier 2/3 artist coalescing above.
            last_heartbeat_at = 0.0

            def _recording_heartbeat(current: int, total: int, mbid: str) -> None:
                nonlocal last_heartbeat_at
                last_heartbeat_at = _heartbeat_with_slow_warning(
                    lambda c, t, m: _emit_pre_pass_heartbeat(ctx, "recordings", c, t, m),
                    last_heartbeat_at,
                    current,
                    total,
                    mbid,
                    phase_label="recordings",
                )

            # A lookup carries the length, the live flag and the performed
            # work. The batched search that created the recording stored the
            # length but no relations, so a recording that has a duration AND
            # sits on a MusicBrainz work has nothing left to learn and is not
            # looked up (a 2026-11-02 profile: 10,936 of 11,166 lookups
            # returned the length we held, while 7,026 named a work the
            # recording was not on).
            musicbrainz_work_ids = repos.works.list_musicbrainz_ids(
                [r.work_id for r in pending_recordings if r.work_id is not None]
            )
            to_look_up = {
                r.id for r in pending_recordings if needs_recording_lookup(r, musicbrainz_work_ids)
            }
            recording_map = mb_client.lookup_recordings(
                sorted(to_look_up), on_progress=_recording_heartbeat
            )
            enrichment_repos = EnrichmentRepos(
                files=repos.library_files,
                enrichment_queries=repos.library_files,
                recordings=repos.recordings,
                works=repos.works,
                song_masters=repos.song_masters,
                matches=repos.matches,
                artists=repos.artists,
            )
            applied_totals = {"works_linked": 0, "files_moved": 0, "marked_live": 0}
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
                needs_lookup = recording.id in to_look_up
                cache_hit = recording.id in recording_map
                cached_payload = recording_map.get(recording.id)
                try:
                    # Map hit -> use cached value (may be None for a 404
                    # that was already authoritative). Map miss -> the
                    # pre-pass failed transiently for this MBID, retry
                    # the lookup live inside this same try/except. A
                    # recording with nothing to learn is not looked up.
                    data: MbRecording | None = None
                    if needs_lookup and cache_hit:
                        data = cached_payload
                    elif needs_lookup:
                        data = mb_client.lookup_recording(recording.id)
                    if data:
                        applied = apply_recording_lookup(recording, data, enrichment_repos)
                        applied_totals["works_linked"] += int(applied.recording_linked)
                        applied_totals["files_moved"] += applied.files_moved
                        applied_totals["marked_live"] += int(applied.marked_live)
                        logger.info(
                            "mb_recording_lookup_applied",
                            mbid=recording.id,
                            work_id=applied.work_id,
                            recording_linked=applied.recording_linked,
                            files_moved=applied.files_moved,
                            marked_live=applied.marked_live,
                            duration_set=applied.duration_set,
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

            ctx.metrics["recordings"] = {
                **_phase_metrics(
                    rows_queued=len(pending_recordings),
                    distinct_mbids=len({r.id for r in pending_recordings}),
                    live_fetches_delta=mb_client.live_fetches - live_start,
                    cache_hits_delta=mb_client.cache_hits - hits_start,
                ),
                "looked_up": len(to_look_up),
                **applied_totals,
            }

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
    independently.  It hands off the local-artist linking, then the targeted matching
    re-check, when it completes.
    """
    settings = get_settings()

    total = 0
    # ctx is created after `total` is computed inside the `task_run` block.
    # `read_progress` (used only for the FAILED payload) and the `finally`
    # summary emission both read ctx.* defensively for the same reason: a
    # mid-loop raise must leave accurate partial values visible even though
    # ctx was created after some of this function's early setup.
    ctx: _PhaseContext | None = None
    task_id = ""

    config = TaskRunConfig(
        task_type=TaskType.MB_ENRICHMENT,
        log_category=LogCategory.ENRICHMENT,
        messages=TaskLifecycleMessages(
            started="mb_enrichment_started",
            completed="mb_enrichment_completed",
            failed="mb_enrichment_failed",
        ),
        read_progress=lambda: (ctx.processed if ctx is not None else 0, total),
    )

    try:
        with task_run(settings.database_url, config) as handle:
            task_id = handle.task_id
            # Pre-count all three queues so `total` is known for the initial
            # RUNNING upsert. Each phase re-opens its own transactional
            # connection below for mutations; this counting connection is
            # read-only.
            with connect_sync(settings.database_url) as counting_conn:
                counting_repos = RepositoryFactory(counting_conn)
                pending_artists = counting_repos.artists.list_unenhanced()
                pending_works = counting_repos.works.list_needing_enhancement()
                pending_recordings = counting_repos.recordings.list_needing_enhancement()

            total = len(pending_artists) + len(pending_works) + len(pending_recordings)

            handle.report_running(
                progress_data={
                    "processed": 0,
                    "total": total,
                    "current_item": "",
                    "phase": "artists",
                }
            )
            handle.log_started(
                details={
                    "artists": len(pending_artists),
                    "works": len(pending_works),
                    "recordings": len(pending_recordings),
                }
            )

            # Single ctx shared across phases. Helpers mutate ctx.processed /
            # ctx.done / ctx.failed / ctx.metrics per row so the outer except
            # handler can read accurate partial values if a phase raises
            # mid-loop.
            ctx = _PhaseContext(
                task_id=handle.task_id,
                task_started_at=handle.started_at,
                total=total,
                progress_repo=handle.progress_repo,
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

            handle.set_completed(
                progress_data={
                    "processed": ctx.processed,
                    "total": total,
                    **completion_details,
                },
                details=completion_details,
            )

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

    artists_done = ctx.done.get("artists", 0)
    artists_failed = ctx.failed.get("artists", 0)
    works_done = ctx.done.get("works", 0)
    works_failed = ctx.failed.get("works", 0)
    recordings_done = ctx.done.get("recordings", 0)
    recordings_failed = ctx.failed.get("recordings", 0)

    logger.info(
        "mb_enrichment_task_complete",
        artists_done=artists_done,
        artists_failed=artists_failed,
        works_done=works_done,
        works_failed=works_failed,
        recordings_done=recordings_done,
        recordings_failed=recordings_failed,
    )

    # Hand off the local-artist linking (AUD-R026, D15), then the targeted re-check (AUD-R022
    # D1, spec 2026-10-05 §4.2). On -w 1 the queue runs them in this order, so the re-check's
    # wave holds the names just linked and matches against their MBIDs. A refused linking
    # enqueue does not stop the re-check. Only after a normal exit: a FAILED run hands off
    # nothing, and the watermark keeps its wave. Guarded (AUD-R012 (1), AUD-R014) on one fresh
    # autocommit connection, as library_enrichment_task does for its own hand-off.
    from backend.tasks.artist_linking_tasks import link_local_artists_task
    from backend.tasks.matching_recheck_tasks import rematch_undecided_task

    with connect_sync(settings.database_url, autocommit=True) as log_conn:
        sys_log_repo = PgSystemLogRepository(log_conn)
        enqueue_or_log(
            link_local_artists_task,
            task_name="link_local_artists_task",
            caller_task_id=task_id,
            log_category=LogCategory.ENRICHMENT,
            sys_log_repo=sys_log_repo,
        )
        enqueue_or_log(
            lambda: rematch_undecided_task(RecheckScope.CHANGED.value),
            task_name="rematch_undecided_task",
            caller_task_id=task_id,
            log_category=LogCategory.ENRICHMENT,
            sys_log_repo=sys_log_repo,
        )

    return {
        "artists_done": artists_done,
        "artists_failed": artists_failed,
        "works_done": works_done,
        "works_failed": works_failed,
        "recordings_done": recordings_done,
        "recordings_failed": recordings_failed,
    }
