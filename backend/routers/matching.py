from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from backend.config import get_settings
from backend.dependencies import get_current_token, get_db_connection, get_mb_client
from backend.domain.enums import MatchStatus, MatchTier, TargetType
from backend.services.identity_resolution_service import (
    LibraryFileNotFoundError,
    persist_manual_match,
    recalculate_for_work_sync,
)
from backend.services.matching_constants import MIN_PRESENTATION_SCORE, QUICK_REVIEW_MIN_SCORE
from backend.services.mb_client import MusicBrainzClientProtocol
from backend.tasks.artist_matching_tasks import artist_matching_task
from backend.tasks.identity_matching_tasks import identity_matching_task

logger = structlog.get_logger()

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]
MbClient = Annotated[MusicBrainzClientProtocol, Depends(get_mb_client)]

# Statuses that are eligible for the review queue
_QUEUE_STATUSES: list[str] = [MatchStatus.NEEDS_REVIEW.value, MatchStatus.PENDING.value]

# Statuses that remain when cascading a MANUAL_REJECTED artist
_PROTECTED_STATUSES: list[str] = [
    MatchStatus.MANUAL_MATCHED.value,
    MatchStatus.MANUAL_REJECTED.value,
]

# Statuses that /matching/run with perform_reset=true rewinds back to PENDING.
# Excludes AUTO_MATCHED (confirmed), MANUAL_MATCHED + MANUAL_REJECTED (curator
# decisions are sacred). PENDING is included so any orphan matches row from a
# crashed prior run is also dropped — see plan section C "write amplification".
_RESETTABLE_IDENTITY_STATUSES: list[str] = [
    MatchStatus.PENDING.value,
    MatchStatus.NEEDS_REVIEW.value,
    MatchStatus.AUTO_REJECTED.value,
]
_RESETTABLE_ARTIST_STATUSES: list[str] = [
    MatchStatus.PENDING.value,
    MatchStatus.NEEDS_REVIEW.value,
    MatchStatus.AUTO_REJECTED.value,
]

# Two-int4 advisory-lock key for /matching/run single-flight protection.
# class_id 0x4D415443 == ASCII 'MATC' (1296126275) reserves a namespace for
# this router; obj_id 1 is the run-lock slot. Future advisory locks under the
# same class can use obj_id 2, 3, ... without collision. Both fit cleanly in
# signed int4 — avoids driver-level bigint coercion edge cases.
_MATCHING_LOCK_CLASS_ID: int = 0x4D415443
_MATCHING_RUN_LOCK_OBJ_ID: int = 1

# Cap on enqueue.failed_playlist_ids returned in the /matching/run response.
# Beyond this, the structured log line is the audit trail.
_MAX_FAILED_PLAYLIST_IDS_IN_RESPONSE: int = 50


# Shared CTE chain for the /queue endpoint. Materialises the artist-level triage
# bucket in SQL so the bucket filter, LIMIT/OFFSET pagination, and the `total`
# count all reference the same filtered set. Thresholds come from
# matching_constants (single source of truth); the f-string interpolates trusted
# module-level ints, never user input.
_QUEUE_BUCKET_CTE = f"""
artist_base AS (
    -- Surface artists in two cases:
    --   1) The artist itself is review-relevant (PENDING / NEEDS_REVIEW), or
    --   2) The artist is resolved (e.g. AUTO_MATCHED) but at least one child
    --      track_identity is review-relevant. Without (2), a curator has no
    --      way to reach a NEEDS_REVIEW child whose parent the matcher already
    --      resolved (the Saliva/"Your Disease" visibility bug, fix #45).
    -- The identity_best CTE below independently filters identities to
    -- _QUEUE_STATUSES, so a resolved parent pulled in only by (2) still
    -- produces the correct triage bucket (its resolved siblings do not
    -- inflate the headline).
    SELECT id, original_name, normalized_name, match_status,
           artist_candidates, reason_code, reason_detail, created_at
    FROM broadcast_artists ba
    WHERE ba.match_status = ANY(%s)
       OR EXISTS (
           SELECT 1 FROM track_identities ti
           WHERE ti.broadcast_artist_id = ba.id
             AND ti.match_status = ANY(%s)
       )
),
identity_best AS (
    -- Mirrors _artist_bucket_from_identities: only review-relevant identities
    -- contribute to the artist bucket so resolved children (AUTO_MATCHED,
    -- MANUAL_*, *_REJECTED) cannot inflate the headline.
    SELECT DISTINCT ON (ti.id)
           ti.id AS identity_id,
           ti.broadcast_artist_id,
           m.confidence_score
    FROM track_identities ti
    LEFT JOIN matches m ON m.identity_id = ti.id
    WHERE ti.broadcast_artist_id IN (SELECT id FROM artist_base)
      AND ti.match_status = ANY(%s)
    ORDER BY ti.id,
             m.confidence_score DESC NULLS LAST,
             m.created_at       DESC NULLS LAST,
             m.id               DESC NULLS LAST
),
artist_bucket AS (
    SELECT a.id,
           CASE
               WHEN bool_or(ib.confidence_score >= {QUICK_REVIEW_MIN_SCORE})
                   THEN 'quick_review'
               WHEN bool_or(ib.confidence_score >= {MIN_PRESENTATION_SCORE}
                            AND ib.confidence_score < {QUICK_REVIEW_MIN_SCORE})
                   THEN 'needs_attention'
               ELSE 'blocked'
           END AS bucket
    FROM artist_base a
    LEFT JOIN identity_best ib ON ib.broadcast_artist_id = a.id
    GROUP BY a.id
)
"""


TriageBucket = Literal["quick_review", "needs_attention", "blocked"]


def _compute_triage_bucket(score: float | None) -> TriageBucket:
    """Single canonical triage implementation. Import and test directly — never
    reimplement.

    score < MIN_PRESENTATION_SCORE is in the token_sort_ratio stopword noise
    band for 2-5-token titles — "blocked" means "nothing useful to show."
    Gap-confirmed mid-band items (score 55-64, gap ≥ 5) are AUTO_MATCHED inside
    strategies and never reach this function.
    """
    if score is None or score < MIN_PRESENTATION_SCORE:
        return "blocked"
    if score >= QUICK_REVIEW_MIN_SCORE:
        return "quick_review"
    return "needs_attention"


def _artist_bucket_from_identities(
    identities: list[QueueIdentity],
) -> TriageBucket:
    """Reduce identity-level triage to an artist-level headline.

    Only identities whose own ``match_status`` is still review-relevant
    (PENDING / NEEDS_REVIEW) contribute to the bucket — resolved children
    (AUTO_MATCHED, MANUAL_*, AUTO_REJECTED) represent work the curator has
    already completed and must not inflate the headline. Within the
    review-relevant subset, the reduction surfaces the most actionable child:
    quick_review > needs_attention > blocked. If there are no review-relevant
    children, the artist is "blocked".
    """
    review_relevant = [i for i in identities if i.match_status in _QUEUE_STATUSES]
    if not review_relevant:
        return "blocked"
    buckets = {i.triage_bucket for i in review_relevant}
    if "quick_review" in buckets:
        return "quick_review"
    if "needs_attention" in buckets:
        return "needs_attention"
    return "blocked"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ProposedMatch(BaseModel):
    """Best-scoring library_file candidate the matcher chose for an identity.

    Surfaced on `needs_review` cards so a curator can approve in one click
    without re-searching for the file the matcher already picked.

    `candidate_match_tier` is the tier on the matches row (i.e. how the
    matcher arrived at this file) and is intentionally distinct from
    QueueIdentity.match_tier (the curator-facing identity tier).
    """

    library_file_id: UUID
    file_path: str
    track_title: str | None = None
    release_title: str | None = None
    recording_mbid: str | None = None
    candidate_match_tier: str


class QueueIdentity(BaseModel):
    """Condensed identity row for the queue response."""

    id: UUID
    original_title: str
    normalized_title: str
    match_status: str
    match_tier: str | None
    confidence_score: float | None = None
    triage_bucket: TriageBucket
    reason_code: str | None = None
    reason_detail: str | None = None
    proposed_match: ProposedMatch | None = Field(
        default=None,
        description=(
            "Best-scoring library_file the matcher chose for this identity, "
            "or null. Reasons it can be null: (1) the matcher has not produced "
            "a candidate yet; (2) no library_file is currently linked to the "
            "persisted match row (orphan FK); (3) Resolution Center safety net "
            "— the persisted match would point at a different artist than the "
            "locked one and is being suppressed. (3) is additive, not breaking; "
            "membership and `total` are unchanged. Note: proposed_match may "
            "remain null when a lower-ranked artist-valid match row exists; "
            "the queue picks the highest-scored row per identity and treats a "
            "wrong-artist row as no proposal."
        ),
    )


class QueueArtist(BaseModel):
    """Artist row for the review queue, including child identities."""

    id: UUID
    original_name: str
    normalized_name: str
    match_status: str
    reason_code: str | None = None
    reason_detail: str | None = None
    triage_bucket: TriageBucket
    candidates: list[dict[str, Any]] | None
    identities: list[QueueIdentity]


class MatchingQueue(BaseModel):
    """Paginated matching queue response."""

    items: list[QueueArtist]
    total: int


class ArtistResolution(BaseModel):
    """Request body for resolving an artist match."""

    match_status: str
    target_artist_id: str | None = None


class IdentityResolution(BaseModel):
    """Request body for resolving an identity match."""

    match_status: str
    library_file_id: UUID | None = None


class ResolveResult(BaseModel):
    """Minimal confirmation of a resolved row."""

    id: UUID
    match_status: str


# ---------------------------------------------------------------------------
# GET /queue
# ---------------------------------------------------------------------------


@router.get("/queue", response_model=MatchingQueue)
async def get_matching_queue(
    conn: DbConn,
    _token: Token,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    bucket: TriageBucket | None = Query(default=None),  # noqa: B008
) -> MatchingQueue:
    """Return paginated artists that need curator review.

    triage_bucket is computed per-identity from confidence_score (sourced via
    LEFT JOIN on matches) and per-artist as the best-of-children reduction.

    The bucket filter (when supplied) is materialised in SQL via a CTE so that
    LIMIT/OFFSET pagination and `total` both reflect the filtered set.

    Locked-artist invariant: a persisted match row whose library_file's
    normalized_artist_name disagrees with the locked broadcast_artist's
    normalized_name is suppressed by the LEFT JOIN predicate. The identity
    still appears in the queue; only proposed_match is masked to null. See
    QueueIdentity.proposed_match for the full enumeration of null reasons.
    """
    page_cur = await conn.execute(
        f"""
        WITH {_QUEUE_BUCKET_CTE},
        filtered AS (
            SELECT a.id, a.original_name, a.normalized_name, a.match_status,
                   a.artist_candidates, a.reason_code, a.reason_detail,
                   a.created_at, ab.bucket
            FROM artist_base a
            JOIN artist_bucket ab ON ab.id = a.id
            WHERE %s::text IS NULL OR ab.bucket = %s::text
        )
        SELECT *, COUNT(*) OVER () AS _total
        FROM filtered
        ORDER BY created_at, id
        LIMIT %s OFFSET %s
        """,
        # Three _QUEUE_STATUSES bindings: artist_base WHERE clause (artist
        # status), artist_base EXISTS subquery (identity status), identity_best
        # WHERE clause (identity status, again).
        (
            _QUEUE_STATUSES,
            _QUEUE_STATUSES,
            _QUEUE_STATUSES,
            bucket,
            bucket,
            limit,
            offset,
        ),
    )
    artist_rows = await page_cur.fetchall()

    if not artist_rows:
        # No rows on this page — issue a standalone count for accurate total.
        count_cur = await conn.execute(
            f"""
            WITH {_QUEUE_BUCKET_CTE}
            SELECT COUNT(*) AS total FROM artist_bucket
            WHERE %s::text IS NULL OR bucket = %s::text
            """,
            # See page query above for binding count rationale.
            (
                _QUEUE_STATUSES,
                _QUEUE_STATUSES,
                _QUEUE_STATUSES,
                bucket,
                bucket,
            ),
        )
        count_row = await count_cur.fetchone()
        total = count_row["total"] if count_row else 0
        return MatchingQueue(items=[], total=total)

    total = artist_rows[0]["_total"]
    artist_ids = [row["id"] for row in artist_rows]

    # Locked-artist invariant for the Resolution Center: a persisted match
    # whose library_files.normalized_artist_name disagrees with the locked
    # broadcast_artists.normalized_name is suppressed by failing the LEFT JOIN
    # predicate. The identity stays visible (LEFT JOIN ⇒ row survives), but
    # ProposedMatch is built only when library_files_joined_id is non-null,
    # so the curator sees no proposal rather than a wrong-artist proposal.
    # SQL NULL note: `=` on a NULL on either side yields NULL, not TRUE — so
    # NULL normalized_artist_name on either side is also fail-closed (same
    # policy as _filter_to_artist in identity_matching_service).
    identities_cur = await conn.execute(
        """
        SELECT DISTINCT ON (ti.id)
               ti.id, ti.broadcast_artist_id, ti.original_title,
               ti.normalized_title, ti.match_status, ti.match_tier,
               ti.reason_code, ti.reason_detail,
               m.confidence_score,
               m.library_file_id,
               m.match_tier AS candidate_match_tier,
               lf.id        AS library_files_joined_id,
               lf.file_path,
               lf.track_title,
               lf.release_title,
               lf.recording_mbid
        FROM track_identities ti
        JOIN broadcast_artists ba   ON ba.id = ti.broadcast_artist_id
        LEFT JOIN matches       m   ON m.identity_id   = ti.id
        LEFT JOIN library_files lf  ON lf.id           = m.library_file_id
                                   AND lf.normalized_artist_name = ba.normalized_name
        WHERE ti.broadcast_artist_id = ANY(%s)
        ORDER BY ti.id,
                 m.confidence_score DESC NULLS LAST,
                 m.created_at       DESC NULLS LAST,
                 m.id               DESC NULLS LAST
        """,
        (artist_ids,),
    )
    identity_rows = await identities_cur.fetchall()

    identities_by_artist: dict[UUID, list[QueueIdentity]] = {}
    for irow in identity_rows:
        aid = irow["broadcast_artist_id"]
        cs: float | None = irow.get("confidence_score")
        # Build proposed_match only when the LEFT JOIN to library_files actually
        # matched a row. Checking library_files_joined_id (rather than
        # m.library_file_id) correctly leaves proposed_match=None for orphan
        # FKs — preserves identity visibility in the queue.
        lf_joined_id = irow.get("library_files_joined_id")
        proposed: ProposedMatch | None = (
            ProposedMatch(
                library_file_id=irow["library_file_id"],
                file_path=irow["file_path"],
                track_title=irow.get("track_title"),
                release_title=irow.get("release_title"),
                recording_mbid=irow.get("recording_mbid"),
                candidate_match_tier=irow["candidate_match_tier"],
            )
            if lf_joined_id is not None
            else None
        )
        identities_by_artist.setdefault(aid, []).append(
            QueueIdentity(
                id=irow["id"],
                original_title=irow["original_title"],
                normalized_title=irow["normalized_title"],
                match_status=irow["match_status"],
                match_tier=irow.get("match_tier"),
                confidence_score=cs,
                triage_bucket=_compute_triage_bucket(cs),
                reason_code=irow.get("reason_code"),
                reason_detail=irow.get("reason_detail"),
                proposed_match=proposed,
            )
        )

    # DISTINCT ON pins SQL ordering to ti.id; restore display-friendly order here.
    for child_list in identities_by_artist.values():
        child_list.sort(key=lambda i: i.original_title)

    items: list[QueueArtist] = []
    for row in artist_rows:
        raw_candidates = row.get("artist_candidates")
        # JSONB may come back as a string depending on the driver version
        if isinstance(raw_candidates, str):
            candidates: list[dict[str, Any]] | None = json.loads(raw_candidates)
        else:
            candidates = raw_candidates
        identities = identities_by_artist.get(row["id"], [])
        artist_bucket = _artist_bucket_from_identities(identities)
        items.append(
            QueueArtist(
                id=row["id"],
                original_name=row["original_name"],
                normalized_name=row["normalized_name"],
                match_status=row["match_status"],
                reason_code=row.get("reason_code"),
                reason_detail=row.get("reason_detail"),
                triage_bucket=artist_bucket,
                candidates=candidates,
                identities=identities,
            )
        )

    return MatchingQueue(items=items, total=total)


# ---------------------------------------------------------------------------
# POST /artists/{artist_id}/resolve
# ---------------------------------------------------------------------------


@router.post(
    "/artists/{artist_id}/resolve",
    response_model=ResolveResult,
)
async def resolve_artist(
    artist_id: UUID,
    body: ArtistResolution,
    conn: DbConn,
    _token: Token,
) -> ResolveResult:
    """Manually resolve an artist as matched or rejected.

    Args:
        artist_id: UUID of the broadcast artist (log entry) to resolve.
        body: Resolution decision.  For ``MANUAL_MATCHED``, ``target_artist_id``
            must be provided.  It accepts **either** a MusicBrainz MBID *or* a
            local catalog UUID (``artists.id``).  The auto-matcher writes both
            forms to ``matches.target_id`` — MBIDs for artists resolved via
            MusicBrainz, local UUIDs for local-only canonicals with no MBID
            (see ``artist_matching_service.py`` exact-pass logic).  Manual
            resolution from the Resolution Center library-search flow sends the
            same dual form: ``artist.mbid ?? artist.id`` (trim-aware).
        conn: Async database connection.
        _token: Bearer token (auth check only).

    Returns:
        :class:`ResolveResult` with updated id and match_status.

    Raises:
        HTTPException: 404 if the artist does not exist.
        HTTPException: 422 if match_status is invalid or target_artist_id is
            missing for MANUAL_MATCHED.
    """
    # Validate match_status
    if body.match_status not in (MatchStatus.MANUAL_MATCHED, MatchStatus.MANUAL_REJECTED):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "match_status must be MANUAL_MATCHED or"
                f" MANUAL_REJECTED, got {body.match_status!r}"
            ),
        )

    if body.match_status == MatchStatus.MANUAL_MATCHED and not body.target_artist_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="target_artist_id is required for MANUAL_MATCHED",
        )

    # Fetch artist
    artist_cur = await conn.execute(
        "SELECT id FROM broadcast_artists WHERE id = %s",
        (artist_id,),
    )
    if await artist_cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artist {artist_id} not found",
        )

    new_status = MatchStatus(body.match_status)

    if new_status == MatchStatus.MANUAL_MATCHED:
        # Update status
        await conn.execute(
            "UPDATE broadcast_artists SET match_status = %s WHERE id = %s",
            (new_status.value, artist_id),
        )
        # Create match row: artist_id → target MBID, confidence=1.0, tier=MANUAL
        match_id = uuid4()
        await conn.execute(
            """
            INSERT INTO matches
                (id, artist_id, target_id, target_type, confidence_score, match_tier)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (artist_id, target_id) DO UPDATE SET
                target_type      = EXCLUDED.target_type,
                confidence_score = EXCLUDED.confidence_score,
                match_tier       = EXCLUDED.match_tier
            """,
            (
                match_id,
                artist_id,
                body.target_artist_id,
                TargetType.ARTIST.value,
                1.0,
                MatchTier.MANUAL.value,
            ),
        )

        # Cascade: reset review-relevant children so the matcher re-runs them
        # against the corrected artist target. Without this, NEEDS_REVIEW
        # children (now visible in the queue via the broadened CTE above)
        # remain stuck — `get_pending_for_playlist` filters strictly to
        # PENDING, so a worker re-run alone wouldn't reprocess them. AUTO_*,
        # MANUAL_*, and *_REJECTED children stay untouched (same philosophy as
        # _PROTECTED_STATUSES on the MANUAL_REJECTED branch below).
        await conn.execute(
            """
            UPDATE track_identities
               SET match_status = %s,
                   match_tier   = NULL,
                   reason_code  = NULL,
                   reason_detail = NULL
             WHERE broadcast_artist_id = %s
               AND match_status = ANY(%s)
            """,
            (
                MatchStatus.PENDING.value,
                artist_id,
                _QUEUE_STATUSES,
            ),
        )
        # Drop stale identity-keyed match rows for ALL children currently
        # PENDING — covers both previously-PENDING children (clearing earlier
        # matcher attempts) and the just-reset NEEDS_REVIEW children. The
        # matcher re-creates fresh ones on the next run.
        await conn.execute(
            """
            DELETE FROM matches
             WHERE identity_id IN (
                 SELECT id FROM track_identities
                  WHERE broadcast_artist_id = %s
                    AND match_status = %s
             )
            """,
            (artist_id, MatchStatus.PENDING.value),
        )
        # Find every playlist that ever played one of this artist's
        # identities, and enqueue identity matching once per playlist.
        # Non-blocking Huey enqueue (mirrors artist_matching_tasks.py:100).
        playlist_cur = await conn.execute(
            """
            SELECT DISTINCT pe.playlist_id
              FROM play_events pe
              JOIN track_identities ti ON ti.id = pe.identity_id
             WHERE ti.broadcast_artist_id = %s
            """,
            (artist_id,),
        )
        playlist_rows = await playlist_cur.fetchall()
        # Commit BEFORE enqueueing. The Huey worker is a separate process
        # with its own DB connection; if the request transaction hasn't
        # committed yet, the worker sees the pre-cascade state (children
        # still NEEDS_REVIEW, stale matches rows present) and skips them
        # entirely (get_pending_for_playlist filters strictly to
        # match_status='pending'). get_db_connection's post-yield commit
        # becomes a no-op after this. Mirrors the commit-before-enqueue
        # ordering in artist_matching_tasks.py:87-100.
        await conn.commit()
        # Enqueue is fire-and-forget across a transaction boundary the
        # request handler has already crossed. The cascade DB work is
        # committed and durable; failing the response would tell the user
        # their manual link failed when it actually succeeded. If the Huey
        # backend is unavailable, the worker's next regular run (or a
        # manual /matching/run) will pick up the now-PENDING children. The
        # broad except is justified at this top-level handler boundary
        # (per error-handling.md) because no specific exception type is
        # reliably knowable across Huey backends (SQLite today, possibly
        # Redis later).
        for row in playlist_rows:
            try:
                identity_matching_task(str(row["playlist_id"]))
            except Exception as exc:  # noqa: BLE001 — top-level boundary; see comment above
                logger.warning(
                    "identity_matching_enqueue_failed",
                    artist_id=str(artist_id),
                    playlist_id=str(row["playlist_id"]),
                    error=repr(exc),
                )
    else:
        # MANUAL_REJECTED: update artist, cascade child identities
        await conn.execute(
            "UPDATE broadcast_artists SET match_status = %s WHERE id = %s",
            (new_status.value, artist_id),
        )
        # Cascade all child identities that are NOT already manually resolved
        await conn.execute(
            """
            UPDATE track_identities
            SET match_status = %s
            WHERE broadcast_artist_id = %s
              AND match_status != ALL(%s)
            """,
            (
                MatchStatus.AUTO_REJECTED.value,
                artist_id,
                list(_PROTECTED_STATUSES),
            ),
        )

    return ResolveResult(id=artist_id, match_status=new_status.value)


# ---------------------------------------------------------------------------
# POST /identities/{identity_id}/resolve
# ---------------------------------------------------------------------------


@router.post(
    "/identities/{identity_id}/resolve",
    response_model=ResolveResult,
)
async def resolve_identity(
    identity_id: UUID,
    body: IdentityResolution,
    conn: DbConn,
    _token: Token,
) -> ResolveResult:
    """Manually resolve a log_identity as matched or rejected.

    Per-song decisions intentionally do not cascade — manually linking one
    identity to a library file says nothing about sibling identities under the
    same broadcast_artist (each song is a distinct match decision). Compare
    with ``resolve_artist``, where MANUAL_MATCHED/MANUAL_REJECTED do cascade
    because the artist mapping is shared by all children.

    Args:
        identity_id: UUID of the log_identity to resolve.
        body: Resolution decision. ``library_file_id`` is required for
            MANUAL_MATCHED.
        conn: Async database connection.
        _token: Bearer token (auth check only).

    Returns:
        :class:`ResolveResult` with updated id and match_status.

    Raises:
        HTTPException: 404 if the identity does not exist.
        HTTPException: 422 if match_status is invalid, library_file_id is
            missing for MANUAL_MATCHED, or library_file_id does not exist.
    """
    if body.match_status not in (MatchStatus.MANUAL_MATCHED, MatchStatus.MANUAL_REJECTED):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "match_status must be MANUAL_MATCHED or"
                f" MANUAL_REJECTED, got {body.match_status!r}"
            ),
        )

    if body.match_status == MatchStatus.MANUAL_MATCHED and not body.library_file_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="library_file_id is required for MANUAL_MATCHED",
        )

    identity_cur = await conn.execute(
        "SELECT id FROM track_identities WHERE id = %s",
        (identity_id,),
    )
    if await identity_cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Identity {identity_id} not found",
        )

    new_status = MatchStatus(body.match_status)

    if new_status == MatchStatus.MANUAL_MATCHED:
        # library_file_id presence is enforced by the 422 guard above; assert
        # for the type-checker so persist_manual_match's UUID parameter is
        # satisfied without a runtime cast.
        assert body.library_file_id is not None  # noqa: S101

        # Update log_identity with status and MANUAL tier
        await conn.execute(
            "UPDATE track_identities SET match_status = %s, match_tier = %s WHERE id = %s",
            (new_status.value, MatchTier.MANUAL.value, identity_id),
        )
        # Delete any existing match for this identity (drops a stale auto
        # match before the manual replacement is inserted, all inside the
        # same async transaction so the resolve is atomic).
        await conn.execute(
            "DELETE FROM matches WHERE identity_id = %s",
            (identity_id,),
        )

        # Insert the new match with derived work_id INSIDE this transaction.
        # 422 if the picked library_file_id doesn't exist, so failure is
        # deterministic instead of waiting for an FK violation to bubble
        # up as 500.
        try:
            work_id = await persist_manual_match(
                conn, identity_id, body.library_file_id
            )
        except LibraryFileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

        # Commit the durable match-state writes BEFORE the cross-connection
        # recalc step. Mirrors resolve_artist's pre-enqueue commit pattern
        # (see line 501) — the post-yield commit in get_db_connection
        # becomes a no-op once this fires.
        await conn.commit()

        if work_id is not None:
            settings = get_settings()
            try:
                # Outer try is defense-in-depth for thread/cancellation
                # boundary errors that recalculate_for_work_sync's internal
                # try cannot reach. Both layers are intentional — do not
                # collapse into one. The match is already committed at this
                # point; recalc is best-effort.
                await asyncio.to_thread(
                    recalculate_for_work_sync,
                    settings.database_url,
                    work_id,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "manual_resolve_recalc_failed",
                    identity_id=str(identity_id),
                    work_id=work_id,
                    exc_info=True,
                )
    else:
        # MANUAL_REJECTED: update status, delete existing match
        await conn.execute(
            "UPDATE track_identities SET match_status = %s, match_tier = %s WHERE id = %s",
            (new_status.value, MatchTier.MANUAL.value, identity_id),
        )
        await conn.execute(
            "DELETE FROM matches WHERE identity_id = %s",
            (identity_id,),
        )

    return ResolveResult(id=identity_id, match_status=new_status.value)


# ---------------------------------------------------------------------------
# POST /run
# ---------------------------------------------------------------------------


class RunMatchingRequest(BaseModel):
    """Body for POST /matching/run.

    perform_reset=False (default): legacy behavior — enqueue artist matching for
    every playlist with an unresolved artist. No state mutation.

    perform_reset=True: also rewind every non-finalized identity and artist
    (PENDING, NEEDS_REVIEW, AUTO_REJECTED) back to PENDING and drop their
    stale matches rows, then enqueue. Manual decisions and AUTO_MATCHED are
    preserved.
    """

    perform_reset: bool = False


async def _try_acquire_run_lock(conn: AsyncConnection[Any]) -> bool:
    """Acquire the SESSION-scoped advisory lock for /matching/run.

    Two-int4 variant: avoids signed-bigint coercion edge cases. The lock
    must span the entire handler (reset transaction + post-commit SELECT +
    Huey enqueue) so a second concurrent run gets 409 across the full
    duration, not just the reset transaction. Caller MUST pair this with a
    finally-block call to ``_release_run_lock`` — otherwise the lock leaks
    onto the pooled connection and persists across subsequent requests
    until that connection is recycled.

    Returns True if acquired, False if a concurrent /matching/run holds it.
    """
    cur = await conn.execute(
        "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
        (_MATCHING_LOCK_CLASS_ID, _MATCHING_RUN_LOCK_OBJ_ID),
    )
    row = await cur.fetchone()
    return bool(row["acquired"]) if row else False


async def _release_run_lock(conn: AsyncConnection[Any]) -> None:
    """Release the session-scoped advisory lock acquired by
    ``_try_acquire_run_lock``. MUST be called in a finally block whenever
    the acquire returned True.

    Cancellation safety: the unlock SQL is wrapped in ``asyncio.shield`` so
    that if this coroutine is cancelled (e.g., the client disconnects
    mid-handler), the underlying ``conn.execute`` still runs to completion
    in the background — Postgres receives the unlock even though we
    propagate ``CancelledError`` to our caller. Without the shield, a
    cancellation arriving during the unlock await would leave the
    session-scoped lock held on the pooled connection, blocking every
    subsequent /matching/run on that connection until it's recycled.

    ``CancelledError`` is intentionally NOT caught — Python 3.8+ classifies
    it under ``BaseException`` (not ``Exception``), so ``except Exception``
    correctly lets it propagate. Catching it would mask cancellation,
    which the FastAPI/asyncio runtime relies on for clean teardown.

    Non-cancellation failures are logged but not re-raised — a leaked lock
    is an operational concern, but masking the original handler response
    with an unlock error would be worse UX.
    """
    try:
        await asyncio.shield(
            conn.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (_MATCHING_LOCK_CLASS_ID, _MATCHING_RUN_LOCK_OBJ_ID),
            )
        )
    except Exception as exc:  # noqa: BLE001 — top-level boundary; CancelledError still propagates
        logger.warning(
            "matching_run_lock_release_failed",
            error=repr(exc),
        )


async def _reset_identities(conn: AsyncConnection[Any]) -> tuple[int, int]:
    """Cohort-scoped identity reset.

    Captures the exact set of rows transitioning back to PENDING via
    UPDATE ... RETURNING, then DELETEs only matches keyed to those IDs.
    Status-based DELETE alone could remove rows that were already PENDING
    before this run. The IS NOT NULL guard on identity_id is defense-in-depth
    against pre-0003-migration deployments that lack the XOR CHECK constraint.

    Also nulls match_tier, reason_code, reason_detail on the reset rows so
    PENDING never carries stale matcher metadata. Mirrors the convention in
    resolve_artist's cascade (matching.py:562-575) — "NULL = no reason for
    pending" is a project-wide invariant, not an opt-in.

    Returns (identities_reset, identity_matches_deleted).
    """
    cur = await conn.execute(
        """
        WITH reset_identities AS (
            UPDATE track_identities
               SET match_status  = %s,
                   match_tier    = NULL,
                   reason_code   = NULL,
                   reason_detail = NULL
             WHERE match_status = ANY(%s)
            RETURNING id
        ),
        deleted_identity_matches AS (
            DELETE FROM matches
             WHERE identity_id IN (SELECT id FROM reset_identities)
               AND identity_id IS NOT NULL
            RETURNING id
        )
        SELECT
            (SELECT COUNT(*) FROM reset_identities)         AS identities_reset,
            (SELECT COUNT(*) FROM deleted_identity_matches) AS identity_matches_deleted
        """,
        (MatchStatus.PENDING.value, _RESETTABLE_IDENTITY_STATUSES),
    )
    row = await cur.fetchone()
    if row is None:
        return 0, 0
    return int(row["identities_reset"]), int(row["identity_matches_deleted"])


async def _reset_artists(conn: AsyncConnection[Any]) -> tuple[int, int]:
    """Cohort-scoped artist reset. Mirrors _reset_identities for the artist
    side. The matches.artist_id IS NOT NULL guard plus the XOR CHECK at
    0003_matching_layer.sql:17-18 ensures identity-keyed match rows are
    untouched.

    Also nulls reason_code / reason_detail on the reset rows. broadcast_artists
    has no match_tier column (per migration 0001 — that lives on
    track_identities and matches only), so only the two reason columns need
    nulling here. artist_candidates is intentionally preserved: the JSONB
    payload of MB search results is potentially expensive to recompute and
    has no "stale" semantics — the next matcher run overwrites it before the
    curator sees it.

    Returns (artists_reset, artist_matches_deleted).
    """
    cur = await conn.execute(
        """
        WITH reset_artists AS (
            UPDATE broadcast_artists
               SET match_status  = %s,
                   reason_code   = NULL,
                   reason_detail = NULL
             WHERE match_status = ANY(%s)
            RETURNING id
        ),
        deleted_artist_matches AS (
            DELETE FROM matches
             WHERE artist_id IN (SELECT id FROM reset_artists)
               AND artist_id IS NOT NULL
            RETURNING id
        )
        SELECT
            (SELECT COUNT(*) FROM reset_artists)         AS artists_reset,
            (SELECT COUNT(*) FROM deleted_artist_matches) AS artist_matches_deleted
        """,
        (MatchStatus.PENDING.value, _RESETTABLE_ARTIST_STATUSES),
    )
    row = await cur.fetchone()
    if row is None:
        return 0, 0
    return int(row["artists_reset"]), int(row["artist_matches_deleted"])


async def _select_playlists_with_pending_artists(
    conn: AsyncConnection[Any],
) -> list[UUID]:
    """Legacy /matching/run selection — playlists with any review-relevant
    artist. Used when perform_reset=False to preserve byte-identical behavior.
    """
    cur = await conn.execute(
        """
        SELECT DISTINCT le.playlist_id
        FROM play_events le
        JOIN track_identities li ON li.id = le.identity_id
        JOIN broadcast_artists la ON la.id = li.broadcast_artist_id
        WHERE la.match_status = ANY(%s)
        """,
        (_QUEUE_STATUSES,),
    )
    rows = await cur.fetchall()
    return [row["playlist_id"] for row in rows]


async def _select_playlists_with_pending_identities_or_artists(
    conn: AsyncConnection[Any],
) -> list[UUID]:
    """Reset-mode selection — playlists touched by any pending artist OR any
    pending identity. After _reset_identities/_reset_artists, all resettable
    rows are PENDING, so both UNION arms hit match_status='pending'. UNION
    (without ALL) deduplicates across arms; per-arm DISTINCT is redundant.
    Explicit-join shape avoids correlated-subquery planner pitfalls.
    """
    cur = await conn.execute(
        """
        SELECT pe.playlist_id
          FROM play_events pe
          JOIN track_identities ti ON ti.id = pe.identity_id
          JOIN broadcast_artists ba ON ba.id = ti.broadcast_artist_id
         WHERE ba.match_status = %s
        UNION
        SELECT pe.playlist_id
          FROM play_events pe
          JOIN track_identities ti ON ti.id = pe.identity_id
         WHERE ti.match_status = %s
        """,
        (MatchStatus.PENDING.value, MatchStatus.PENDING.value),
    )
    rows = await cur.fetchall()
    return [row["playlist_id"] for row in rows]


def _enqueue_playlists(playlist_ids: list[UUID]) -> list[UUID]:
    """Fire-and-forget Huey enqueue for each playlist. Collects per-playlist
    failures (Huey backend transient errors) and returns them so the response
    can surface partial failures. Mirrors the resolve_artist enqueue contract
    at the top-level boundary — bare-except is justified because no specific
    exception type is reliably knowable across Huey backends.
    """
    failures: list[UUID] = []
    for pid in playlist_ids:
        try:
            artist_matching_task(str(pid))
        except Exception as exc:  # noqa: BLE001 — top-level fire-and-forget boundary
            logger.warning(
                "matching_run_enqueue_failed",
                playlist_id=str(pid),
                error=repr(exc),
            )
            failures.append(pid)
    return failures


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def run_matching(
    conn: DbConn,
    _token: Token,
    body: RunMatchingRequest = Body(default_factory=RunMatchingRequest),  # noqa: B008
) -> dict[str, Any]:
    """Trigger artist-matching tasks across playlists.

    Default (perform_reset=False): finds all playlists with at least one
    artist in PENDING/NEEDS_REVIEW state and fires a Huey artist-matching
    task per playlist. Identities under already-resolved artists are NOT
    re-evaluated; existing matches rows are NOT deleted.

    With perform_reset=True: first rewinds every non-finalized identity and
    artist (PENDING, NEEDS_REVIEW, AUTO_REJECTED) back to PENDING and drops
    their matches rows, then enqueues every playlist that now has pending
    work. AUTO_MATCHED, MANUAL_MATCHED, and MANUAL_REJECTED rows are
    preserved untouched. Single-flight via Postgres advisory lock — a
    concurrent reset returns 409.

    Returns a structured response with reset and enqueue counts.
    """
    start_ms = time.monotonic()
    identities_reset = identity_matches_deleted = 0
    artists_reset = artist_matches_deleted = 0
    lock_held = False

    try:
        if body.perform_reset:
            # Acquire BEFORE the transaction so the lock spans the reset
            # commit AND the post-commit SELECT + enqueue. A second
            # concurrent /run reaching any of those phases gets 409.
            lock_held = await _try_acquire_run_lock(conn)
            if not lock_held:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="matching_run_in_progress",
                )
            # Reset runs in its own transaction so the row writes commit
            # before enqueue (matches the codebase's commit-before-enqueue
            # contract — Huey workers on a separate connection must see
            # the committed PENDING state, not a snapshot mid-reset).
            async with conn.transaction():
                identities_reset, identity_matches_deleted = await _reset_identities(conn)
                artists_reset, artist_matches_deleted = await _reset_artists(conn)
            playlist_ids = await _select_playlists_with_pending_identities_or_artists(conn)
        else:
            playlist_ids = await _select_playlists_with_pending_artists(conn)

        failed_playlist_ids = _enqueue_playlists(playlist_ids)
    finally:
        # Always release if held — including on the HTTPException path above
        # (won't fire there since we raise before lock_held is set True) and
        # any unexpected error during reset/select/enqueue. Skipped when
        # acquire returned False so the contending request doesn't unlock
        # the lock the FIRST request still holds.
        if lock_held:
            await _release_run_lock(conn)

    duration_ms = int((time.monotonic() - start_ms) * 1000)
    matches_deleted = identity_matches_deleted + artist_matches_deleted
    queued = len(playlist_ids) - len(failed_playlist_ids)

    logger.info(
        "matching_run_complete",
        perform_reset=body.perform_reset,
        identities_reset=identities_reset,
        artists_reset=artists_reset,
        matches_deleted=matches_deleted,
        playlists_queued=queued,
        enqueue_failures=len(failed_playlist_ids),
        duration_ms=duration_ms,
    )

    return {
        "status": "accepted",
        "reset": {
            "performed": body.perform_reset,
            "identities_reset": identities_reset,
            "artists_reset": artists_reset,
            "matches_deleted": matches_deleted,
        },
        "enqueue": {
            "playlists_queued": queued,
            "enqueue_failures": len(failed_playlist_ids),
            "failed_playlist_ids": sorted(str(pid) for pid in failed_playlist_ids)[
                :_MAX_FAILED_PLAYLIST_IDS_IN_RESPONSE
            ],
        },
    }


# ---------------------------------------------------------------------------
# GET /mb-artists
# ---------------------------------------------------------------------------


@router.get("/mb-artists")
async def search_mb_artists(
    _token: Token,
    mb_client: MbClient,
    query: str = Query(min_length=1, max_length=100),
) -> dict[str, Any]:
    """Search MusicBrainz for artists matching query string.

    Proxies to MusicBrainzApiClient.search_artist() with local cache
    (read-through via PgMusicBrainzCacheRepository). Used by the frontend
    when no automated candidates exist (PR 5 — ArtistPanel "Search
    MusicBrainz" empty-state CTA).

    Wiring rationale: MusicBrainzApiClient is fully synchronous (httpx.Client
    + sync psycopg).  The endpoint injects it via a sync generator dependency
    (get_mb_client) that opens a dedicated sync psycopg connection — identical
    to the Huey task pattern (artist_matching_tasks.py).  The search call is
    dispatched to a thread pool via asyncio.to_thread so it does not block the
    event loop.  The dependency itself opens/closes the connection around the
    request lifetime.
    """
    items = await asyncio.to_thread(mb_client.search_artist, query)
    return {"items": items}
