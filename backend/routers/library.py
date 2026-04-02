from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import get_current_token, get_db_connection
from backend.tasks.library_tasks import library_scan_task

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


# ---------------------------------------------------------------------------
# Schemas — scan (existing)
# ---------------------------------------------------------------------------


class ScanRequest(BaseModel):
    root_path: str


# ---------------------------------------------------------------------------
# Schemas — status
# ---------------------------------------------------------------------------


class LibraryStatus(BaseModel):
    total_files: int
    quarantine_count: int
    by_format: dict[str, int]
    by_enrichment: dict[str, int]


# ---------------------------------------------------------------------------
# Schemas — artists
# ---------------------------------------------------------------------------


class ArtistSummary(BaseModel):
    id: str
    name: str
    sort_name: str
    disambiguation: str | None
    work_count: int
    file_count: int


class PaginatedArtists(BaseModel):
    items: list[ArtistSummary]
    total: int


# ---------------------------------------------------------------------------
# Schemas — artist detail
# ---------------------------------------------------------------------------


class WorkSummary(BaseModel):
    id: str
    title: str
    recording_count: int
    has_master: bool


class ArtistDetail(BaseModel):
    id: str
    name: str
    sort_name: str
    disambiguation: str | None
    works: list[WorkSummary]


# ---------------------------------------------------------------------------
# Schemas — work detail
# ---------------------------------------------------------------------------


class FileInfo(BaseModel):
    id: UUID
    file_path: str
    format: str
    bitrate: int | None
    duration_ms: int | None
    track_title: str | None
    release_title: str | None
    enrichment_status: str


class RecordingDetail(BaseModel):
    id: str
    title: str
    version_type: str
    duration_ms: int | None
    files: list[FileInfo]


class SongMasterInfo(BaseModel):
    id: UUID
    preferred_file_id: UUID
    selection_method: str
    score: int | None
    updated_at: datetime


class FormatOverrideInfo(BaseModel):
    id: UUID
    format_name: str
    preferred_file_id: UUID
    notes: str | None
    created_at: datetime


class WorkDetail(BaseModel):
    id: str
    title: str
    artist_id: str
    recordings: list[RecordingDetail]
    song_master: SongMasterInfo | None
    format_overrides: list[FormatOverrideInfo]


# ---------------------------------------------------------------------------
# Schemas — master management
# ---------------------------------------------------------------------------


class SetMasterRequest(BaseModel):
    preferred_file_id: UUID


class SongMasterResponse(BaseModel):
    id: UUID
    work_id: str
    preferred_file_id: UUID
    selection_method: str
    score: int | None
    updated_at: datetime


# ---------------------------------------------------------------------------
# Schemas — format overrides management
# ---------------------------------------------------------------------------


class CreateFormatOverrideRequest(BaseModel):
    format_name: str
    preferred_file_id: UUID
    notes: str | None = None


class FormatOverrideResponse(BaseModel):
    id: UUID
    work_id: str
    format_name: str
    preferred_file_id: UUID
    notes: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Routes — existing
# ---------------------------------------------------------------------------


@router.post("/scan", status_code=status.HTTP_202_ACCEPTED)
async def scan_library(body: ScanRequest) -> dict[str, str]:
    """Enqueue a background library scan for the given directory."""
    library_scan_task(body.root_path)
    return {"status": "accepted", "message": f"Library scan queued for {body.root_path}"}


# ---------------------------------------------------------------------------
# Routes — status
# ---------------------------------------------------------------------------


@router.get("/status", response_model=LibraryStatus)
async def get_library_status(conn: DbConn, _token: Token) -> LibraryStatus:
    """Return aggregate counts: total files, quarantined files, by format, by enrichment."""
    total_cur = await conn.execute("SELECT COUNT(*) AS cnt FROM library_files")
    total_row = await total_cur.fetchone()
    total_files: int = total_row["cnt"] if total_row else 0

    q_cur = await conn.execute("SELECT COUNT(*) AS cnt FROM library_quarantine")
    q_row = await q_cur.fetchone()
    quarantine_count: int = q_row["cnt"] if q_row else 0

    fmt_cur = await conn.execute(
        "SELECT format, COUNT(*) AS cnt FROM library_files GROUP BY format"
    )
    fmt_rows = await fmt_cur.fetchall()
    by_format: dict[str, int] = {r["format"]: r["cnt"] for r in fmt_rows}

    enr_cur = await conn.execute(
        "SELECT enrichment_status, COUNT(*) AS cnt FROM library_files GROUP BY enrichment_status"
    )
    enr_rows = await enr_cur.fetchall()
    by_enrichment: dict[str, int] = {r["enrichment_status"]: r["cnt"] for r in enr_rows}

    return LibraryStatus(
        total_files=total_files,
        quarantine_count=quarantine_count,
        by_format=by_format,
        by_enrichment=by_enrichment,
    )


# ---------------------------------------------------------------------------
# Routes — artists (paginated)
# ---------------------------------------------------------------------------


@router.get("/artists", response_model=PaginatedArtists)
async def list_artists(
    conn: DbConn,
    _token: Token,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    search: str = Query(default=""),
) -> PaginatedArtists:
    """Return a paginated list of artists with work and file counts.

    Args:
        conn: Async database connection.
        _token: Auth token (validated by dependency).
        limit: Page size (1-500, default 50).
        offset: Number of items to skip.
        search: Optional case-insensitive substring filter on artist name.

    Returns:
        :class:`PaginatedArtists` with items and total count.
    """
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
            COUNT(DISTINCT w.id)  AS work_count,
            COUNT(DISTINCT lf.id) AS file_count
        FROM artists a
        LEFT JOIN works      w  ON w.artist_id  = a.id
        LEFT JOIN recordings r  ON r.work_id    = w.id
        LEFT JOIN library_files lf ON lf.recording_id = r.id
        {where_clause}
        GROUP BY a.id, a.name, a.sort_name, a.disambiguation
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
        )
        for row in rows
    ]
    return PaginatedArtists(items=items, total=total)


# ---------------------------------------------------------------------------
# Routes — artist detail
# ---------------------------------------------------------------------------


@router.get("/artists/{artist_id}", response_model=ArtistDetail)
async def get_artist_detail(
    artist_id: str, conn: DbConn, _token: Token
) -> ArtistDetail:
    """Return an artist with a summary of their works.

    Args:
        artist_id: The artist MBID.
        conn: Async database connection.
        _token: Auth token.

    Returns:
        :class:`ArtistDetail` with nested work summaries.

    Raises:
        HTTPException: 404 if the artist does not exist.
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

    works_cur = await conn.execute(
        """
        SELECT
            w.id,
            w.title,
            COUNT(DISTINCT r.id)  AS recording_count,
            COUNT(DISTINCT sm.id) AS master_count
        FROM works w
        LEFT JOIN recordings r  ON r.work_id = w.id
        LEFT JOIN song_masters sm ON sm.work_id = w.id
        WHERE w.artist_id = %s
        GROUP BY w.id, w.title
        ORDER BY w.title
        """,
        (artist_id,),
    )
    work_rows = await works_cur.fetchall()

    works = [
        WorkSummary(
            id=row["id"],
            title=row["title"],
            recording_count=row["recording_count"],
            has_master=row["master_count"] > 0,
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


# ---------------------------------------------------------------------------
# Routes — work detail
# ---------------------------------------------------------------------------


@router.get("/works/{work_id}", response_model=WorkDetail)
async def get_work_detail(
    work_id: str, conn: DbConn, _token: Token
) -> WorkDetail:
    """Return a work with its recordings, files, master, and format overrides.

    Args:
        work_id: The work MBID.
        conn: Async database connection.
        _token: Auth token.

    Returns:
        :class:`WorkDetail` with nested recording and file details.

    Raises:
        HTTPException: 404 if the work does not exist.
    """
    work_cur = await conn.execute(
        "SELECT id, title, artist_id FROM works WHERE id = %s", (work_id,)
    )
    work_row = await work_cur.fetchone()
    if work_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Work {work_id} not found",
        )

    # Recordings + their files (single JOIN query)
    rec_cur = await conn.execute(
        """
        SELECT
            r.id            AS rec_id,
            r.title         AS rec_title,
            r.version_type  AS rec_version_type,
            r.duration_ms   AS rec_duration_ms,
            lf.id           AS file_id,
            lf.file_path,
            lf.format,
            lf.bitrate,
            lf.duration_ms  AS file_duration_ms,
            lf.track_title,
            lf.release_title,
            lf.enrichment_status
        FROM recordings r
        LEFT JOIN library_files lf ON lf.recording_id = r.id
        WHERE r.work_id = %s
        ORDER BY r.id, lf.file_path
        """,
        (work_id,),
    )
    rec_rows = await rec_cur.fetchall()

    # Group by recording
    recordings_map: dict[str, dict[str, Any]] = {}
    for row in rec_rows:
        rid = row["rec_id"]
        if rid not in recordings_map:
            recordings_map[rid] = {
                "id": rid,
                "title": row["rec_title"],
                "version_type": row["rec_version_type"] or "ORIGINAL",
                "duration_ms": row["rec_duration_ms"],
                "files": [],
            }
        if row["file_id"] is not None:
            recordings_map[rid]["files"].append(
                FileInfo(
                    id=row["file_id"],
                    file_path=row["file_path"],
                    format=row["format"],
                    bitrate=row.get("bitrate"),
                    duration_ms=row.get("file_duration_ms"),
                    track_title=row.get("track_title"),
                    release_title=row.get("release_title"),
                    enrichment_status=row["enrichment_status"],
                )
            )

    recordings = [
        RecordingDetail(
            id=rec["id"],
            title=rec["title"],
            version_type=rec["version_type"],
            duration_ms=rec["duration_ms"],
            files=rec["files"],
        )
        for rec in recordings_map.values()
    ]

    # Song master
    sm_cur = await conn.execute(
        "SELECT * FROM song_masters WHERE work_id = %s", (work_id,)
    )
    sm_row = await sm_cur.fetchone()
    song_master: SongMasterInfo | None = None
    if sm_row is not None:
        song_master = SongMasterInfo(
            id=sm_row["id"],
            preferred_file_id=sm_row["preferred_file_id"],
            selection_method=sm_row["selection_method"],
            score=sm_row.get("score"),
            updated_at=sm_row["updated_at"],
        )

    # Format overrides
    fo_cur = await conn.execute(
        "SELECT * FROM format_overrides WHERE work_id = %s ORDER BY format_name",
        (work_id,),
    )
    fo_rows = await fo_cur.fetchall()
    format_overrides = [
        FormatOverrideInfo(
            id=row["id"],
            format_name=row["format_name"],
            preferred_file_id=row["preferred_file_id"],
            notes=row.get("notes"),
            created_at=row["created_at"],
        )
        for row in fo_rows
    ]

    return WorkDetail(
        id=work_row["id"],
        title=work_row["title"],
        artist_id=work_row["artist_id"],
        recordings=recordings,
        song_master=song_master,
        format_overrides=format_overrides,
    )


# ---------------------------------------------------------------------------
# Routes — master management
# ---------------------------------------------------------------------------


@router.put("/works/{work_id}/master", response_model=SongMasterResponse)
async def set_work_master(
    work_id: str, body: SetMasterRequest, conn: DbConn, _token: Token
) -> SongMasterResponse:
    """Manually set the preferred file for a work (UPSERT).

    Args:
        work_id: The work MBID.
        body: Request body containing ``preferred_file_id``.
        conn: Async database connection.
        _token: Auth token.

    Returns:
        The current :class:`SongMasterResponse` after upsert.

    Raises:
        HTTPException: 404 if the work does not exist.
    """
    work_cur = await conn.execute(
        "SELECT id FROM works WHERE id = %s", (work_id,)
    )
    if await work_cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Work {work_id} not found",
        )

    master_id = uuid4()
    await conn.execute(
        """
        INSERT INTO song_masters
            (id, work_id, preferred_file_id, selection_method, score, updated_at)
        VALUES (%s, %s, %s, 'manual', NULL, now())
        ON CONFLICT (work_id) DO UPDATE SET
            preferred_file_id = EXCLUDED.preferred_file_id,
            selection_method  = 'manual',
            score             = NULL,
            updated_at        = now()
        """,
        (master_id, work_id, body.preferred_file_id),
    )

    row_cur = await conn.execute(
        "SELECT * FROM song_masters WHERE work_id = %s", (work_id,)
    )
    row = await row_cur.fetchone()
    assert row is not None
    return SongMasterResponse(
        id=row["id"],
        work_id=row["work_id"],
        preferred_file_id=row["preferred_file_id"],
        selection_method=row["selection_method"],
        score=row.get("score"),
        updated_at=row["updated_at"],
    )


@router.delete("/works/{work_id}/master", status_code=status.HTTP_204_NO_CONTENT)
async def delete_work_master(
    work_id: str, conn: DbConn, _token: Token
) -> None:
    """Remove the manual song master for a work.

    Args:
        work_id: The work MBID.
        conn: Async database connection.
        _token: Auth token.

    Raises:
        HTTPException: 404 if no master exists for this work.
    """
    cur = await conn.execute(
        "SELECT id FROM song_masters WHERE work_id = %s", (work_id,)
    )
    if await cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No master found for work {work_id}",
        )
    await conn.execute("DELETE FROM song_masters WHERE work_id = %s", (work_id,))


# ---------------------------------------------------------------------------
# Routes — format overrides
# ---------------------------------------------------------------------------


@router.post(
    "/works/{work_id}/format-overrides",
    response_model=FormatOverrideResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_format_override(
    work_id: str, body: CreateFormatOverrideRequest, conn: DbConn, _token: Token
) -> FormatOverrideResponse:
    """Create a per-format preferred file override for a work.

    Args:
        work_id: The work MBID.
        body: Request body with ``format_name``, ``preferred_file_id``, and optional ``notes``.
        conn: Async database connection.
        _token: Auth token.

    Returns:
        The created :class:`FormatOverrideResponse`.

    Raises:
        HTTPException: 404 if the work does not exist.
    """
    work_cur = await conn.execute(
        "SELECT id FROM works WHERE id = %s", (work_id,)
    )
    if await work_cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Work {work_id} not found",
        )

    override_id = uuid4()
    await conn.execute(
        """
        INSERT INTO format_overrides (id, work_id, format_name, preferred_file_id, notes)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (override_id, work_id, body.format_name, body.preferred_file_id, body.notes),
    )

    row_cur = await conn.execute(
        "SELECT * FROM format_overrides WHERE id = %s", (override_id,)
    )
    row = await row_cur.fetchone()
    assert row is not None
    return FormatOverrideResponse(
        id=row["id"],
        work_id=row["work_id"],
        format_name=row["format_name"],
        preferred_file_id=row["preferred_file_id"],
        notes=row.get("notes"),
        created_at=row["created_at"],
    )


@router.delete(
    "/works/{work_id}/format-overrides/{override_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_format_override(
    work_id: str, override_id: UUID, conn: DbConn, _token: Token
) -> None:
    """Delete a format override by ID.

    Args:
        work_id: The work MBID (used to scope the lookup).
        override_id: The UUID of the format override to remove.
        conn: Async database connection.
        _token: Auth token.

    Raises:
        HTTPException: 404 if the override does not exist for this work.
    """
    cur = await conn.execute(
        "SELECT id FROM format_overrides WHERE id = %s AND work_id = %s",
        (override_id, work_id),
    )
    if await cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Format override {override_id} not found for work {work_id}",
        )
    await conn.execute("DELETE FROM format_overrides WHERE id = %s", (override_id,))
