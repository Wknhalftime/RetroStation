from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.db.repositories.artist_merges import ARTIST_GROUP_SQL, MERGE_ROOTS_CTE
from backend.db.repositories.version_entries import AsyncVersionEntryReader
from backend.dependencies import SyncRepos, get_current_token, get_db_connection
from backend.domain.catalog import (
    SPECIAL_PURPOSE_MBIDS,
    ArtistMerge,
    MergeArtistNotFoundError,
    MergeRefusedError,
)
from backend.domain.synthetic_work_id import encode as encode_synthetic_work_id
from backend.domain.system import StorageUnavailableError
from backend.services.artist_merge_service import merge_artist, unmerge_artist
from backend.services.version_entry_service import new_counts

router = APIRouter()

# Trusted constants, never user input.
_PLACEHOLDER_MBIDS_SQL = ", ".join(f"'{mbid}'" for mbid in sorted(SPECIAL_PURPOSE_MBIDS))
# A listed artist: named (an unnamed stub's name is its MBID, phase 1a S15), no special-purpose
# artist (phase 1c S7, C7), and not merged into another (phase 2a S11, M33). ``IS DISTINCT FROM``,
# because a local artist's ``mbid`` is NULL.
_NAMED = (
    "a.name IS DISTINCT FROM a.mbid"
    f" AND (a.mbid IS NULL OR a.mbid NOT IN ({_PLACEHOLDER_MBIDS_SQL}))"
    " AND NOT EXISTS (SELECT 1 FROM artist_merges merged WHERE merged.artist_id = a.id)"
)
# The artist an MBID finds: the artist holding it, or the primary that artist is merged into
# (M33: lookups by either MBID land there). Reads ``merge_roots``, from MERGE_ROOTS_CTE.
_BY_MBID = """a.id IN (
    SELECT COALESCE(r.primary_id, h.id)
      FROM artists h LEFT JOIN merge_roots r ON r.artist_id = h.id
     WHERE h.mbid = %s)"""


def _artist_search_fragments(
    search: str, mbid: str = ""
) -> tuple[str, str, tuple[Any, ...], tuple[Any, ...]]:
    """Build the SQL fragments and parameter lists for an artist name or MBID search.

    Returns ``(where_clause, order_clause, where_params, order_params)`` so
    that both the count and items queries always use the identical predicate —
    making drift between the two impossible.

    ``where_params`` feeds the WHERE clause (1 bind for an MBID, 2 more when searching).
    ``order_params`` feeds the similarity() call in ORDER BY (1 bind when
    searching, 0 otherwise) and is only needed by the items query.

    psycopg note: ``%%`` in a Python SQL string literal becomes a single ``%``
    after psycopg's placeholder pass, so ``LOWER(a.name) %% LOWER(%s)`` reaches
    Postgres as the trigram ``%`` operator.  The GIN index on ``LOWER(name)``
    (migration 0022) makes this efficient.  ``similarity()`` is used only in
    ORDER BY for ranking — not in WHERE — so the index is exercised for
    filtering via the ``%%`` arm.

    An unnamed stub (phase 1a S15: its name is still its MBID) is never listed, and neither
    is one of MusicBrainz's special-purpose artists (phase 1c S7), by its MBID, nor an artist
    merged into another (phase 2a S11) (``_NAMED``). Both queries read ``merge_roots``.
    """
    where = f"WHERE {_NAMED}"
    where_params: tuple[Any, ...] = ()
    if mbid:
        where += f" AND {_BY_MBID}"
        where_params = (mbid.lower(),)
    if not search:
        return where, "ORDER BY a.sort_name, a.id", where_params, ()

    where += " AND (LOWER(a.name) %% LOWER(%s) OR LOWER(a.name) LIKE LOWER(%s) || '%%')"
    order = "ORDER BY similarity(LOWER(a.name), LOWER(%s)) DESC, a.sort_name, a.id"
    return where, order, (*where_params, search, search), (search,)


DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


class ArtistSummary(BaseModel):
    id: str
    name: str
    sort_name: str
    disambiguation: str | None
    work_count: int
    file_count: int
    mbid: str | None = None
    origin: str = "local"
    new_version_count: int = 0


class PaginatedArtists(BaseModel):
    items: list[ArtistSummary]
    total: int


class WorkSummary(BaseModel):
    id: str
    title: str
    recording_count: int
    has_master: bool
    mbid: str | None = None
    origin: str = "local"
    version_types: list[str] = []
    new_version_count: int = 0


class MergedMember(BaseModel):
    """An artist merged into a primary's group; ``primary_id`` is the one it was merged into
    directly, so the page can say who leaves with it on Unmerge."""

    id: str
    name: str
    mbid: str | None
    primary_id: str


class ArtistDetail(BaseModel):
    id: str
    name: str
    sort_name: str
    disambiguation: str | None
    works: list[WorkSummary]
    merged_into: str | None = None
    members: list[MergedMember] = []
    new_version_count: int = 0


class MergeIn(BaseModel):
    primary_id: str
    reason: str | None = None


@router.get("/artists", response_model=PaginatedArtists)
async def list_artists(
    conn: DbConn,
    _token: Token,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    search: str = Query(default=""),
    mbid: str = Query(default=""),
) -> PaginatedArtists:
    """Return a paginated list of artists with work and file counts.

    When ``search`` is non-empty, results are filtered and ranked by trigram
    similarity; see :func:`_artist_search_fragments` for the SQL construction
    details and the migration-0022 GIN index that makes it efficient. ``mbid`` finds the
    artist holding it, or its primary. A primary counts its merge group's works and files.
    """
    where_clause, order_clause, where_params, order_params = _artist_search_fragments(search, mbid)

    count_sql = f"""
        WITH RECURSIVE {MERGE_ROOTS_CTE}
        SELECT COUNT(*) AS total
        FROM artists a
        {where_clause}
    """
    count_params: tuple[Any, ...] = where_params
    cnt_cur = await conn.execute(count_sql, count_params)
    cnt_row = await cnt_cur.fetchone()
    total: int = cnt_row["total"] if cnt_row else 0

    items_sql = f"""
        WITH RECURSIVE {MERGE_ROOTS_CTE}
        SELECT
            a.id,
            a.name,
            a.sort_name,
            a.disambiguation,
            a.mbid,
            a.origin,
            COUNT(DISTINCT w.id)  AS work_count,
            COUNT(DISTINCT lf.id) AS file_count
        FROM artists a
        LEFT JOIN (
            -- each work under the root of its artist's group: the artist itself, unmerged
            SELECT gw.id, COALESCE(r.primary_id, gw.artist_id) AS root_id
              FROM works gw LEFT JOIN merge_roots r ON r.artist_id = gw.artist_id
        ) w ON w.root_id = a.id
        LEFT JOIN library_files lf ON lf.work_id = w.id
        {where_clause}
        GROUP BY a.id, a.name, a.sort_name, a.disambiguation,
                 a.mbid, a.origin
        {order_clause}
        LIMIT %s OFFSET %s
    """
    items_params: tuple[Any, ...] = where_params + order_params + (limit, offset)

    items_cur = await conn.execute(items_sql, items_params)
    rows = await items_cur.fetchall()

    page_works = await _page_work_ids(conn, [str(row["id"]) for row in rows])
    counts = await new_counts(
        AsyncVersionEntryReader(conn), sorted({w for ws in page_works.values() for w in ws})
    )
    items = [
        ArtistSummary(
            id=row["id"],
            name=row["name"],
            sort_name=row["sort_name"],
            disambiguation=row.get("disambiguation"),
            work_count=row["work_count"],
            file_count=row["file_count"],
            mbid=row.get("mbid"),
            origin=row.get("origin", "local"),
            new_version_count=sum(counts.get(w, 0) for w in page_works.get(str(row["id"]), [])),
        )
        for row in rows
    ]
    return PaginatedArtists(items=items, total=total)


# The works of a page of listed artists, each under the root of its artist's group (the list's
# own rule): read by works.artist_id for the page's artists and their merged members only.
_PAGE_WORKS_SQL = f"""
    WITH RECURSIVE {MERGE_ROOTS_CTE}
    SELECT COALESCE(r.primary_id, w.artist_id) AS root_id, w.id
      FROM works w
      LEFT JOIN merge_roots r ON r.artist_id = w.artist_id
     WHERE (w.artist_id = ANY(%(ids)s)
            OR w.artist_id IN (SELECT m.artist_id FROM merge_roots m
                                WHERE m.primary_id = ANY(%(ids)s)))
       AND COALESCE(r.primary_id, w.artist_id) = ANY(%(ids)s)
"""


async def _page_work_ids(conn: AsyncConnection[Any], artist_ids: list[str]) -> dict[str, list[str]]:
    """New-version counts (D5) need each listed artist's works; read for the page only, so the
    list's own query keeps its two counts."""
    if not artist_ids:
        return {}
    cur = await conn.execute(_PAGE_WORKS_SQL, {"ids": artist_ids})
    found: dict[str, list[str]] = {}
    for row in await cur.fetchall():
        found.setdefault(str(row["root_id"]), []).append(str(row["id"]))
    return found


_GROUP_WORKS_SQL = """
    SELECT
        w.id,
        w.title,
        w.mbid,
        w.origin,
        w.artist_id,
        COUNT(DISTINCT lf.id) AS recording_count,
        COUNT(DISTINCT sm.id) AS master_count,
        array_agg(DISTINCT r.version_type)
            FILTER (WHERE r.version_type IS NOT NULL)
            AS version_types
    FROM works w
    LEFT JOIN library_files lf ON lf.work_id = w.id
    LEFT JOIN song_masters sm ON sm.work_id = w.id
    LEFT JOIN recordings r ON r.work_id = w.id
    WHERE w.artist_id = ANY(%s)
    GROUP BY w.id, w.title, w.mbid, w.origin, w.artist_id
    ORDER BY w.title
"""

# Files by MBID, one entry per (MBID, title) holding a file on none of the given works: an artist
# with no works shows its files this way, and so does a merged member (2a's question A). An entry
# counts every file of its MBID and title, because its link opens them all (``works.py``'s
# synthetic work detail).
_FILES_BY_MBID_SQL = """
    SELECT m.mbid, lf.track_title AS title, COUNT(DISTINCT lf.id) AS recording_count
      FROM unnest(%(mbids)s::text[]) AS m(mbid)
      JOIN library_files lf ON lf.artist_mbid = m.mbid OR lf.album_artist_mbid = m.mbid
     WHERE lf.track_title IS NOT NULL
     GROUP BY m.mbid, lf.track_title
    HAVING bool_or(lf.work_id IS NULL OR NOT lf.work_id = ANY(%(works)s::text[]))
     ORDER BY lf.track_title, m.mbid
"""


async def _group_works(
    conn: AsyncConnection[Any], member_ids: list[str]
) -> tuple[list[WorkSummary], set[str]]:
    """The works of every member of the group, by title, and the members that own one."""
    cur = await conn.execute(_GROUP_WORKS_SQL, (member_ids,))
    rows = await cur.fetchall()
    works = [
        WorkSummary(
            id=row["id"],
            title=row["title"],
            recording_count=row["recording_count"],
            has_master=row["master_count"] > 0,
            mbid=row.get("mbid"),
            origin=row.get("origin", "local"),
            version_types=row.get("version_types") or [],
        )
        for row in rows
    ]
    return works, {str(row["artist_id"]) for row in rows}


async def _files_by_mbid(
    conn: AsyncConnection[Any], mbids: list[str], works: list[WorkSummary]
) -> list[WorkSummary]:
    """One synthetic work per (MBID, title) of the files carrying *mbids* and on none of
    *works*; its id opens those files (``works.py``'s synthetic work detail)."""
    if not mbids:
        return []
    cur = await conn.execute(
        _FILES_BY_MBID_SQL, {"mbids": mbids, "works": [work.id for work in works]}
    )
    return [
        WorkSummary(
            id=encode_synthetic_work_id(row["mbid"], row["title"] or "unknown"),
            title=row["title"] or "Unknown",
            recording_count=row["recording_count"],
            has_master=False,
        )
        for row in await cur.fetchall()
    ]


@router.get("/artists/{artist_id}", response_model=ArtistDetail)
async def get_artist_detail(artist_id: str, conn: DbConn, _token: Token) -> ArtistDetail:
    """Return an artist with a summary of its works.

    Phase 2a S11 (M33): a merged artist names its group's root primary in ``merged_into`` (the
    page redirects there), and a root lists its ``members``. The page shows the whole group's
    works, then each member's files that aren't on one of them, by the member's MBID. The
    primary's own files show that way too while it owns no work, as an artist with no works
    always has.
    """
    artist_cur = await conn.execute(
        "SELECT id, name, sort_name, disambiguation FROM artists WHERE id = %s",
        (artist_id,),
    )
    artist_row = await artist_cur.fetchone()
    if artist_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artist {artist_id} not found",
        )

    group_cur = await conn.execute(ARTIST_GROUP_SQL, {"id": artist_id})
    group = await group_cur.fetchall()
    primary_id = next((str(row["id"]) for row in group if row["is_primary"]), None)
    if primary_id is None:  # deleted since the first read
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artist {artist_id} not found",
        )
    works, owners = await _group_works(conn, [str(row["id"]) for row in group])
    by_file = sorted(
        str(row["mbid"])
        for row in group
        if row["mbid"] and not (row["is_primary"] and str(row["id"]) in owners)
    )
    counts = await new_counts(AsyncVersionEntryReader(conn), [w.id for w in works])
    works = [w.model_copy(update={"new_version_count": counts.get(w.id, 0)}) for w in works]
    works += await _files_by_mbid(conn, by_file, works)

    is_primary = primary_id == artist_id
    return ArtistDetail(
        id=artist_row["id"],
        name=artist_row["name"],
        sort_name=artist_row["sort_name"],
        disambiguation=artist_row.get("disambiguation"),
        works=works,
        merged_into=None if is_primary else primary_id,
        new_version_count=sum(counts.values()),
        members=[
            MergedMember(
                id=row["id"], name=row["name"], mbid=row["mbid"], primary_id=row["primary_id"]
            )
            for row in group
            if is_primary and not row["is_primary"]
        ],
    )


# ---------------------------------------------------------------------------
# Routes — writes: sync routes on the request's one connection (SyncRepos, C75), the curator's
# Merge and Unmerge (phase 2a S11, M33); nothing else merges
# ---------------------------------------------------------------------------


@router.post("/artists/{artist_id}/merge", status_code=status.HTTP_204_NO_CONTENT)
def merge_artist_into(_token: Token, artist_id: str, body: MergeIn, repos: SyncRepos) -> None:
    """Merge the artist into ``primary_id``; a repeat changes nothing."""
    merge = ArtistMerge(artist_id=artist_id, primary_id=body.primary_id, reason=body.reason)
    try:
        merge_artist(merge, repos.artist_merges)
    except MergeArtistNotFoundError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from missing
    except MergeRefusedError as refused:
        raise HTTPException(status.HTTP_409_CONFLICT, str(refused)) from refused
    except StorageUnavailableError as lost:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable") from lost


@router.delete("/artists/{artist_id}/merge", status_code=status.HTTP_204_NO_CONTENT)
def unmerge_artist_from(_token: Token, artist_id: str, repos: SyncRepos) -> None:
    """Unmerge the artist: delete its one edge; an artist with none is left as it is."""
    try:
        unmerge_artist(artist_id, repos.artist_merges)
    except MergeArtistNotFoundError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from missing
    except StorageUnavailableError as lost:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable") from lost
