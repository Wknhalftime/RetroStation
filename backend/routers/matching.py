from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from pydantic import BaseModel, Field

from backend.config import get_settings
from backend.dependencies import get_current_token, get_db_connection, get_mb_client
from backend.domain.enums import MatchStatus, MatchTier, ReasonCode, TargetType
from backend.domain.matching import RecheckNotQueuedError
from backend.domain.system import StorageUnavailableError
from backend.services.identity_resolution_service import (
    IdentityNotFoundError,
    LibraryFileNotFoundError,
    SongNotRejectableError,
    SuggestionRequiredError,
    persist_manual_match,
    recalculate_for_work_sync,
    reject_song_suggestion,
    unmatch_song,
)
from backend.services.matching_constants import MIN_PRESENTATION_SCORE, QUICK_REVIEW_MIN_SCORE
from backend.services.mb_client import MusicBrainzClientProtocol
from backend.services.repository_factory import recalc_repos
from backend.tasks.identity_matching_tasks import identity_matching_task
from backend.tasks.matching_recheck_tasks import queue_recheck_all

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

# Source statuses eligible for /unmatch. Pending and needs_review are not
# "unmatchable" — they have nothing to revert. Manual children are NOT
# protected on the artist cascade path (intentional: unmatching an artist
# wipes the slate for every child regardless of status).
_UNMATCHABLE_STATUSES: list[str] = [
    MatchStatus.AUTO_MATCHED.value,
    MatchStatus.MANUAL_MATCHED.value,
    MatchStatus.AUTO_REJECTED.value,
    MatchStatus.MANUAL_REJECTED.value,
]


_USER_UNMATCHED: str = ReasonCode.USER_UNMATCHED.value
_LIBRARY_FILE_REMOVED: str = ReasonCode.LIBRARY_FILE_REMOVED.value

# Shared CTE chain for the /queue endpoint. Materialises the artist-level triage
# bucket in SQL so the bucket filter, LIMIT/OFFSET pagination, and the `total`
# count all reference the same filtered set. Thresholds come from
# matching_constants (single source of truth); the f-string interpolates trusted
# module-level ints and enum values, never user input.
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
    -- The search predicate sits here (vs. the page query) so pagination,
    -- the `total` window count, and the empty-page count fallback all see
    -- the same filtered artist set. Special LIKE chars in the user's term
    -- (percent, underscore, and the bang we use as the escape marker) are
    -- pre-escaped by _normalize_search; the ESCAPE '!' clause then makes
    -- them match literally.
    SELECT id, original_name, normalized_name, match_status,
           artist_candidates, reason_code, reason_detail, created_at
    FROM broadcast_artists ba
    WHERE (ba.match_status = ANY(%s)
        OR EXISTS (
            SELECT 1 FROM track_identities ti
            WHERE ti.broadcast_artist_id = ba.id
              AND ti.match_status = ANY(%s)
        ))
      AND (%s::text IS NULL OR ba.original_name ILIKE '%%' || %s || '%%' ESCAPE '!')
),
identity_best AS (
    -- Mirrors _artist_bucket_from_identities: only review-relevant identities
    -- contribute to the artist bucket so resolved children (AUTO_MATCHED,
    -- MANUAL_*, *_REJECTED) cannot inflate the headline.
    SELECT DISTINCT ON (ti.id)
           ti.id AS identity_id,
           ti.broadcast_artist_id,
           ti.reason_code,
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
           END AS bucket,
           -- Likely = worth a curator's time by default: a review item with a
           -- guess at the presentation floor, or one whose match was taken away:
           -- by the curator (unmatch) or by deleting its missing library file.
           -- Both delete the match rows, so it has no score, and the artist must
           -- not vanish from the default queue.
           COALESCE(bool_or(ib.confidence_score >= {MIN_PRESENTATION_SCORE}
                            OR ib.reason_code IN ('{_USER_UNMATCHED}',
                                                  '{_LIBRARY_FILE_REMOVED}')), FALSE)
               OR COALESCE(bool_or(a.reason_code = '{_USER_UNMATCHED}'), FALSE)
               AS likely
    FROM artist_base a
    LEFT JOIN identity_best ib ON ib.broadcast_artist_id = a.id
    GROUP BY a.id
)
"""


TriageBucket = Literal["quick_review", "needs_attention", "blocked"]


# Sort modes for the queue endpoint. Mapped to a static ORDER BY clause —
# values are inlined into the page-query f-string, so any new mode added here
# must be a trusted SQL fragment, never user input.
QueueSort = Literal["created_at", "name"]

_SORT_CLAUSES: dict[str, str] = {
    "created_at": "ORDER BY created_at, id",
    "name": "ORDER BY LOWER(original_name), id",
}


# Pre-escape characters that ILIKE treats as metacharacters so a user's literal
# `%`, `_`, or `!` matches itself rather than acting as a wildcard / escape
# trigger. Pairs with `ESCAPE '!'` in the artist_base predicate. `!` is chosen
# over `\` to sidestep PostgreSQL's standard_conforming_strings ambiguity for
# backslash literals — `'!' ` is the same single character regardless of mode.
def _normalize_search(raw: str | None) -> str | None:
    if raw is None:
        return None
    trimmed = raw.strip()
    if not trimmed:
        return None
    return trimmed.replace("!", "!!").replace("%", "!%").replace("_", "!_")


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
            "locked one and is being suppressed; (4) the best guess scores "
            "below MIN_PRESENTATION_SCORE (triage_bucket 'blocked'), where it "
            "is the artist's nearest title, not a proposal — confidence_score "
            "still carries the score. (3) and (4) are additive, not breaking; "
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
    unlikely_total: int = Field(
        default=0,
        description=(
            "Artists matching the search and bucket filters whose review "
            "items have no guess at MIN_PRESENTATION_SCORE or above. Counted "
            "whether or not include_unlikely lets them into `items`, so the "
            "UI can say how many it is hiding."
        ),
    )


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
    search: str | None = Query(default=None, max_length=200),  # noqa: B008
    sort: QueueSort = Query(default="created_at"),  # noqa: B008
    include_unlikely: bool = Query(default=True),  # noqa: B008
) -> MatchingQueue:
    """Return paginated artists that need curator review.

    triage_bucket is computed per-identity from confidence_score (sourced via
    LEFT JOIN on matches) and per-artist as the best-of-children reduction.

    The bucket filter (when supplied) is materialised in SQL via a CTE so that
    LIMIT/OFFSET pagination and `total` both reflect the filtered set.

    `search` is a case-insensitive substring match against
    broadcast_artists.original_name. It is applied inside artist_base (the
    same place as the queue-status filter) so pagination, the `total` window
    count, and the empty-page count fallback all reference the same filtered
    set. `sort=name` orders alphabetically by original_name; default is the
    historical created_at, id order.

    `include_unlikely=false` leaves out artists with no likely match (see
    `likely` in _QUEUE_BUCKET_CTE); `unlikely_total` counts them either way.

    Locked-artist invariant: a persisted match row whose library_file's
    normalized_artist_name disagrees with the locked broadcast_artist's
    normalized_name is suppressed by the LEFT JOIN predicate. The identity
    still appears in the queue; only proposed_match is masked to null. See
    QueueIdentity.proposed_match for the full enumeration of null reasons.
    """
    search_term = _normalize_search(search)
    order_by = _SORT_CLAUSES[sort]
    page_cur = await conn.execute(
        f"""
        WITH {_QUEUE_BUCKET_CTE},
        filtered AS (
            SELECT a.id, a.original_name, a.normalized_name, a.match_status,
                   a.artist_candidates, a.reason_code, a.reason_detail,
                   a.created_at, ab.bucket, ab.likely
            FROM artist_base a
            JOIN artist_bucket ab ON ab.id = a.id
            WHERE %s::text IS NULL OR ab.bucket = %s::text
        )
        SELECT *, COUNT(*) OVER () AS _total,
               (SELECT COUNT(*) FROM filtered WHERE NOT likely) AS _unlikely_total
        FROM filtered
        WHERE %s OR likely
        {order_by}
        LIMIT %s OFFSET %s
        """,
        # Bind order, top-to-bottom through the SQL:
        #   artist_base WHERE       — _QUEUE_STATUSES (artist status)
        #   artist_base EXISTS      — _QUEUE_STATUSES (identity status)
        #   artist_base name guard  — search_term (NULL check)
        #   artist_base ILIKE       — search_term (pattern)
        #   identity_best WHERE     — _QUEUE_STATUSES (identity status, again)
        #   filtered WHERE NULL     — bucket
        #   filtered WHERE eq       — bucket
        #   page WHERE              — include_unlikely
        #   LIMIT / OFFSET          — limit / offset
        (
            _QUEUE_STATUSES,
            _QUEUE_STATUSES,
            search_term,
            search_term,
            _QUEUE_STATUSES,
            bucket,
            bucket,
            include_unlikely,
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
            SELECT COUNT(*) FILTER (WHERE %s OR likely) AS total,
                   COUNT(*) FILTER (WHERE NOT likely)   AS unlikely_total
            FROM artist_bucket
            WHERE %s::text IS NULL OR bucket = %s::text
            """,
            # Same artist_base/identity_best bind order as the page query,
            # then include_unlikely for the total's FILTER and the two
            # `bucket` bindings for the count's WHERE.
            (
                _QUEUE_STATUSES,
                _QUEUE_STATUSES,
                search_term,
                search_term,
                _QUEUE_STATUSES,
                include_unlikely,
                bucket,
                bucket,
            ),
        )
        count_row = await count_cur.fetchone()
        return MatchingQueue(
            items=[],
            total=count_row["total"] if count_row else 0,
            unlikely_total=count_row["unlikely_total"] if count_row else 0,
        )

    total = artist_rows[0]["_total"]
    unlikely_total = artist_rows[0]["_unlikely_total"]
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
        triage_bucket = _compute_triage_bucket(cs)
        # Build proposed_match only when the LEFT JOIN to library_files actually
        # matched a row. Checking library_files_joined_id (rather than
        # m.library_file_id) correctly leaves proposed_match=None for orphan
        # FKs — preserves identity visibility in the queue. A blocked guess is
        # the artist's nearest title, not a proposal.
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
            if lf_joined_id is not None and triage_bucket != "blocked"
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
                triage_bucket=triage_bucket,
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

    return MatchingQueue(items=items, total=total, unlikely_total=unlikely_total)


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
                f"match_status must be MANUAL_MATCHED or MANUAL_REJECTED, got {body.match_status!r}"
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
        # Non-blocking Huey enqueue (mirrors the end of artist_matching_task).
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
        # ordering in artist_matching_task.
        await conn.commit()
        # Enqueue is fire-and-forget across a transaction boundary the
        # request handler has already crossed. The cascade DB work is
        # committed and durable; failing the response would tell the user
        # their manual link failed when it actually succeeded. If the Huey
        # backend is unavailable, the worker's next regular run (or a
        # manual /matching/run) will pick up the now-PENDING children. The
        # broad except is justified at this top-level handler boundary
        # because no specific exception type is
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
        HTTPException: 409 if the song is already rejected.
        HTTPException: 422 if a song in review is rejected without library_file_id.
    """
    if body.match_status not in (MatchStatus.MANUAL_MATCHED, MatchStatus.MANUAL_REJECTED):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"match_status must be MANUAL_MATCHED or MANUAL_REJECTED, got {body.match_status!r}"
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
            work_id = await persist_manual_match(conn, identity_id, body.library_file_id)
        except LibraryFileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

        # Commit the durable match-state writes BEFORE the cross-connection
        # recalc step. Mirrors resolve_artist's commit-before-enqueue
        # pattern — the post-yield commit in get_db_connection
        # becomes a no-op once this fires.
        await conn.commit()

        if work_id is not None:
            settings = get_settings()
            try:
                # recalculate_for_work_sync's own try only swallows
                # psycopg.Error (a transient DB problem — safe to retry via
                # the next matching run). This outer try is the backstop for
                # everything else that can come out of that call: a non-DB
                # bug in the recalc itself, plus thread/cancellation
                # boundary errors the inner try can't reach either way.
                # Both layers are intentional — do not collapse into one.
                # The match is already committed at this point; recalc is
                # best-effort regardless of which layer catches the failure.
                await asyncio.to_thread(
                    recalculate_for_work_sync,
                    settings.database_url,
                    work_id,
                    recalc_repos,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "manual_resolve_recalc_failed",
                    identity_id=str(identity_id),
                    work_id=work_id,
                    exc_info=True,
                )
    else:
        # Reject rules out this (song, work) pair (AUD-R022, D3/D5): the song goes back to review
        # with the file recorded, and the matcher skips it and its work from now on.
        try:
            await reject_song_suggestion(conn, identity_id, body.library_file_id)
        except IdentityNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except SuggestionRequiredError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        except SongNotRejectableError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return ResolveResult(id=identity_id, match_status=MatchStatus.NEEDS_REVIEW.value)

    return ResolveResult(id=identity_id, match_status=new_status.value)


# ---------------------------------------------------------------------------
# POST /artists/{artist_id}/unmatch
# ---------------------------------------------------------------------------


@router.post(
    "/artists/{artist_id}/unmatch",
    response_model=ResolveResult,
)
async def unmatch_artist(
    artist_id: UUID,
    conn: DbConn,
    _token: Token,
) -> ResolveResult:
    """Revert a finalized artist match back to NEEDS_REVIEW.

    Cascades to ALL child identities regardless of status — every child is
    set to NEEDS_REVIEW with reason_code=USER_UNMATCHED, and every match row
    keyed to either the artist or any of its children is deleted. This is
    intentionally more aggressive than ``resolve_artist``'s
    MANUAL_REJECTED cascade (which protects manual children); unmatching an
    artist is the curator saying "throw the whole tree out and start over."

    Raises:
        HTTPException: 404 if the artist does not exist.
        HTTPException: 409 if the artist's current ``match_status`` is not
            in :data:`_UNMATCHABLE_STATUSES` (i.e. PENDING / NEEDS_REVIEW).
    """
    needs_review = MatchStatus.NEEDS_REVIEW.value
    user_unmatched = ReasonCode.USER_UNMATCHED.value

    # Atomic gate: a single UPDATE...WHERE id AND match_status closes the
    # SELECT-then-UPDATE TOCTOU window where a concurrent /resolve could
    # transition the row between our read and our write. RETURNING tells us
    # whether the gate matched; on no-match we issue a separate SELECT solely
    # to distinguish 404 (row gone) from 409 (row exists, wrong state).
    artist_cur = await conn.execute(
        """
        UPDATE broadcast_artists
           SET match_status  = %s,
               reason_code   = %s,
               reason_detail = NULL
         WHERE id = %s
           AND match_status = ANY(%s)
        RETURNING id
        """,
        (needs_review, user_unmatched, artist_id, _UNMATCHABLE_STATUSES),
    )
    if await artist_cur.fetchone() is None:
        existing = await conn.execute(
            "SELECT match_status FROM broadcast_artists WHERE id = %s",
            (artist_id,),
        )
        existing_row = await existing.fetchone()
        if existing_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Artist {artist_id} not found",
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Artist {artist_id} is in {existing_row['match_status']!r}; "
                "only finalized matches can be unmatched"
            ),
        )
    await conn.execute(
        """
        UPDATE track_identities
           SET match_status  = %s,
               match_tier    = NULL,
               reason_code   = %s,
               reason_detail = NULL
         WHERE broadcast_artist_id = %s
        """,
        (needs_review, user_unmatched, artist_id),
    )
    await conn.execute(
        """
        DELETE FROM matches
         WHERE artist_id = %s
            OR identity_id IN (
                SELECT id FROM track_identities
                 WHERE broadcast_artist_id = %s
            )
        """,
        (artist_id, artist_id),
    )

    return ResolveResult(id=artist_id, match_status=needs_review)


# ---------------------------------------------------------------------------
# POST /identities/{identity_id}/unmatch
# ---------------------------------------------------------------------------


@router.post(
    "/identities/{identity_id}/unmatch",
    response_model=ResolveResult,
)
async def unmatch_identity(
    identity_id: UUID,
    conn: DbConn,
    _token: Token,
) -> ResolveResult:
    """Revert a finalized identity match back to NEEDS_REVIEW.

    Per-song unmatch — does not touch sibling identities or the parent
    artist. Mirrors the asymmetry in ``resolve_identity`` (no cascade).
    A matched song's file(s) are recorded as rejected (AUD-R022, D5).

    Raises:
        HTTPException: 404 if the identity does not exist.
        HTTPException: 409 if the identity's current ``match_status`` is
            not in :data:`backend.services.identity_resolution_service._UNMATCHABLE`.
    """
    try:
        await unmatch_song(conn, identity_id)
    except IdentityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SongNotRejectableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return ResolveResult(id=identity_id, match_status=MatchStatus.NEEDS_REVIEW.value)


# ---------------------------------------------------------------------------
# POST /run
# ---------------------------------------------------------------------------


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def run_matching(_token: Token) -> dict[str, bool]:
    """Queue a re-check of every undecided artist and song (the Re-run Matching button).

    The worker's ``rematch_undecided_task("all")`` rewinds undecided items to pending and hands
    every playlist with pending work to artist matching (AUD-R022 D1, spec 2026-10-05 §4.2).
    Manual decisions and auto-matches are never touched (D2). Any request body is ignored.

    Raises:
        HTTPException: 503 if the task queue refuses the job.
    """
    try:
        queue_recheck_all()
    except RecheckNotQueuedError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not queue the re-check; try again shortly.",
        ) from exc
    return {"queued": True}


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

    A lost database connection (the cache's ``StorageUnavailableError``) answers 503.
    """
    try:
        items = await asyncio.to_thread(mb_client.search_artist, query)
    except StorageUnavailableError as lost:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="unavailable"
        ) from lost
    return {"items": items}
