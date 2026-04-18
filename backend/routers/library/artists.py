from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import get_current_token, get_db_connection
from backend.domain.synthetic_work_id import encode as encode_synthetic_work_id

router = APIRouter()

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


class ArtistDetail(BaseModel):
    id: str
    name: str
    sort_name: str
    disambiguation: str | None
    works: list[WorkSummary]


@router.get("/artists", response_model=PaginatedArtists)
async def list_artists(
    conn: DbConn,
    _token: Token,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    search: str = Query(default=""),
) -> PaginatedArtists:
    """Return a paginated list of artists with work and file counts."""
    where_clause = "WHERE LOWER(a.name) LIKE LOWER(%s)" if search else ""
    search_param = f"%{search}%" if search else None

    count_sql = f"""
        SELECT COUNT(DISTINCT a.id) AS total
        FROM artists a
        {where_clause}
    """
    count_params: tuple[Any, ...] = (search_param,) if search else ()
    cnt_cur = await conn.execute(count_sql, count_params)
    cnt_row = await cnt_cur.fetchone()
    total: int = cnt_row["total"] if cnt_row else 0

    items_sql = f"""
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
        LEFT JOIN works w ON w.artist_id = a.id
        LEFT JOIN library_files lf ON lf.work_id = w.id
        {where_clause}
        GROUP BY a.id, a.name, a.sort_name, a.disambiguation,
                 a.mbid, a.origin
        ORDER BY a.sort_name
        LIMIT %s OFFSET %s
    """
    items_params: tuple[Any, ...] = (
        (search_param, limit, offset) if search else (limit, offset)
    )

    items_cur = await conn.execute(items_sql, items_params)
    rows = await items_cur.fetchall()

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
        )
        for row in rows
    ]
    return PaginatedArtists(items=items, total=total)


@router.get("/artists/{artist_id}", response_model=ArtistDetail)
async def get_artist_detail(
    artist_id: str, conn: DbConn, _token: Token
) -> ArtistDetail:
    """Return an artist with a summary of their works."""
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

    works_cur = await conn.execute(
        """
        SELECT
            w.id,
            w.title,
            w.mbid,
            w.origin,
            COUNT(DISTINCT lf.id) AS recording_count,
            COUNT(DISTINCT sm.id) AS master_count,
            array_agg(DISTINCT r.version_type)
                FILTER (WHERE r.version_type IS NOT NULL)
                AS version_types
        FROM works w
        LEFT JOIN library_files lf ON lf.work_id = w.id
        LEFT JOIN song_masters sm ON sm.work_id = w.id
        LEFT JOIN recordings r ON r.work_id = w.id
        WHERE w.artist_id = %s
        GROUP BY w.id, w.title, w.mbid, w.origin
        ORDER BY w.title
        """,
        (artist_id,),
    )
    work_rows = await works_cur.fetchall()

    if not work_rows:
        works_cur = await conn.execute(
            """
            SELECT
                lf.track_title AS title,
                COUNT(*) AS recording_count
            FROM library_files lf
            WHERE (lf.album_artist_mbid = %s OR lf.artist_mbid = %s)
              AND lf.track_title IS NOT NULL
            GROUP BY lf.track_title
            ORDER BY lf.track_title
            """,
            (artist_id, artist_id),
        )
        work_rows = await works_cur.fetchall()
        works = [
            WorkSummary(
                id=encode_synthetic_work_id(artist_id, row["title"] or "unknown"),
                title=row["title"] or "Unknown",
                recording_count=row["recording_count"],
                has_master=False,
            )
            for row in work_rows
        ]
    else:
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
            for row in work_rows
        ]

    return ArtistDetail(
        id=artist_row["id"],
        name=artist_row["name"],
        sort_name=artist_row["sort_name"],
        disambiguation=artist_row.get("disambiguation"),
        works=works,
    )
