from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from pydantic import BaseModel

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
    proposed_match: ProposedMatch | None = None


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
        LEFT JOIN matches       m  ON m.identity_id   = ti.id
        LEFT JOIN library_files lf ON lf.id           = m.library_file_id
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


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def run_matching(
    conn: DbConn,
    _token: Token,
) -> dict[str, Any]:
    """Trigger artist-matching tasks for all playlists with unresolved artists.

    Finds all playlists that have at least one log_artist in PENDING or
    NEEDS_REVIEW state (via track_identities → play_events join), then fires a
    background Huey task for each.

    Args:
        conn: Async database connection.
        _token: Bearer token (auth check only).

    Returns:
        A dict with a confirmation message and the number of tasks queued.
    """
    playlists_cur = await conn.execute(
        """
        SELECT DISTINCT le.playlist_id
        FROM play_events le
        JOIN track_identities li ON li.id = le.identity_id
        JOIN broadcast_artists la ON la.id = li.broadcast_artist_id
        WHERE la.match_status = ANY(%s)
        """,
        (_QUEUE_STATUSES,),
    )
    rows = await playlists_cur.fetchall()

    for row in rows:
        artist_matching_task(str(row["playlist_id"]))

    count = len(rows)
    return {
        "status": "accepted",
        "message": f"Artist matching queued for {count} playlist(s)",
        "count": count,
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
