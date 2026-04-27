from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import get_current_token, get_db_connection

router = APIRouter()


def _file_search_fragments(
    search: str,
    artist_mbid: str | None,
) -> tuple[str, str, tuple[Any, ...], tuple[Any, ...]]:
    """Build the SQL fragments and parameter lists for a file search.

    Returns ``(where_clause, order_clause, where_params, order_params)`` so
    that both the count and items queries always use the identical predicate —
    making drift between the two impossible.

    ``where_params`` feeds the WHERE clause.
    ``order_params`` feeds the similarity() call in ORDER BY and is only
    needed by the items query.

    psycopg note: ``%%`` in a Python SQL string literal becomes a single ``%``
    after psycopg's placeholder pass, so ``LOWER(track_title) %% LOWER(%s)``
    reaches Postgres as the trigram ``%`` operator.  The GIN partial index on
    ``LOWER(track_title)`` (migration 0023, WHERE track_title IS NOT NULL)
    makes this efficient.  ``similarity()`` is used only in ORDER BY for
    ranking — not in WHERE — so the index is exercised for filtering via the
    ``%%`` arm.
    """
    conditions: list[str] = []
    where_params: list[Any] = []

    if search:
        conditions.append(
            "track_title IS NOT NULL AND "
            "(LOWER(track_title) %% LOWER(%s) OR LOWER(track_title) LIKE LOWER(%s) || '%%')"
        )
        where_params.extend([search, search])

    if artist_mbid:
        conditions.append("(album_artist_mbid = %s OR artist_mbid = %s)")
        where_params.extend([artist_mbid, artist_mbid])

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    if search:
        order_clause = (
            "ORDER BY similarity(LOWER(track_title), LOWER(%s)) DESC, track_title, id"
        )
        order_params: tuple[Any, ...] = (search,)
    else:
        order_clause = "ORDER BY track_title NULLS LAST, id"
        order_params = ()

    return where_clause, order_clause, tuple(where_params), order_params


DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


class LibraryFileSummary(BaseModel):
    id: UUID
    file_path: str
    track_title: str | None


class PaginatedLibraryFiles(BaseModel):
    items: list[LibraryFileSummary]
    total: int


@router.get("/files", response_model=PaginatedLibraryFiles)
async def list_files(
    conn: DbConn,
    _token: Token,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str = Query(default=""),
    artist_mbid: str | None = Query(default=None),
) -> PaginatedLibraryFiles:
    """Return a paginated list of library files, optionally filtered by search
    query and/or artist MBID.

    When ``search`` is non-empty, results are filtered and ranked by trigram
    similarity on ``track_title``; see :func:`_file_search_fragments` for the
    SQL construction details and the migration-0023 GIN partial index.

    When ``artist_mbid`` is provided, only files whose ``album_artist_mbid``
    or ``artist_mbid`` column matches are returned.  The two filters are
    combined with AND when both are present.
    """
    where_clause, order_clause, where_params, order_params = _file_search_fragments(
        search, artist_mbid
    )

    count_sql = f"""
        SELECT COUNT(*) AS total
        FROM library_files
        {where_clause}
    """
    cnt_cur = await conn.execute(count_sql, where_params)
    cnt_row = await cnt_cur.fetchone()
    total: int = cnt_row["total"] if cnt_row else 0

    items_sql = f"""
        SELECT id, file_path, track_title
        FROM library_files
        {where_clause}
        {order_clause}
        LIMIT %s OFFSET %s
    """
    items_params: tuple[Any, ...] = where_params + order_params + (limit, offset)
    items_cur = await conn.execute(items_sql, items_params)
    rows = await items_cur.fetchall()

    items = [
        LibraryFileSummary(
            id=row["id"],
            file_path=row["file_path"],
            track_title=row.get("track_title"),
        )
        for row in rows
    ]
    return PaginatedLibraryFiles(items=items, total=total)
