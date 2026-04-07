from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.config import get_settings
from backend.dependencies import get_current_token, get_db_connection
from backend.domain.enums import Origin
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
    mbid: str | None = None
    origin: str = "local"


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
    mbid: str | None = None
    origin: str = "local"


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
async def scan_library(body: ScanRequest, _token: Token) -> dict[str, str]:
    """Enqueue a background library scan for the given directory."""
    scan_path = Path(body.root_path).resolve()

    # Validate against configured allowlist
    settings = get_settings()
    allowed = [Path(p).resolve() for p in settings.library_scan_paths]
    if allowed and not any(
        scan_path == p or scan_path.is_relative_to(p) for p in allowed
    ):
        raise HTTPException(
            status_code=403,
            detail="Path not in allowed scan paths",
        )

    if not scan_path.exists() or not scan_path.is_dir():
        raise HTTPException(status_code=400, detail="Invalid directory path")
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
            a.mbid,
            a.origin,
            COUNT(DISTINCT w.id)  AS work_count,
            COUNT(DISTINCT lf.id) AS file_count
        FROM artists a
        LEFT JOIN works      w  ON w.artist_id  = a.id
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
    if row is None:
        raise RuntimeError("Expected row after INSERT")
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
    if row is None:
        raise RuntimeError("Expected row after INSERT")
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


# ---------------------------------------------------------------------------
# Schemas — merge / split / reassign
# ---------------------------------------------------------------------------


class MergeRequest(BaseModel):
    source_work_ids: list[str]


class MergeResponse(BaseModel):
    merged_file_count: int
    deleted_work_count: int
    dropped_override_count: int


class SplitRequest(BaseModel):
    file_id: UUID


class SplitResponse(BaseModel):
    new_work_id: str
    old_work_deleted: bool


class ReassignRequest(BaseModel):
    work_id: str


class ReassignResponse(BaseModel):
    old_work_id: str | None
    old_work_deleted: bool


# ---------------------------------------------------------------------------
# Routes — merge / split / reassign
# ---------------------------------------------------------------------------


async def _recalculate_song_master(conn: AsyncConnection[Any], work_id: str) -> None:
    """Recalculate the auto song master for a work using async SQL.

    Skips recalculation if a manual master already exists for the work.
    Upserts the best-scored file as the new auto master.
    """
    # Skip if a manual selection exists
    sm_cur = await conn.execute(
        "SELECT selection_method FROM song_masters WHERE work_id = %s",
        (work_id,),
    )
    sm_row = await sm_cur.fetchone()
    if sm_row is not None and sm_row["selection_method"] == "manual":
        return

    # Gather all files for this work via recording chain
    files_cur = await conn.execute(
        """
        SELECT
            lf.id,
            lf.release_status,
            lf.release_type,
            lf.format,
            lf.bitrate,
            lf.duration_ms
        FROM library_files lf
        JOIN recordings r ON lf.recording_id = r.id
        WHERE r.work_id = %s
        """,
        (work_id,),
    )
    file_rows = await files_cur.fetchall()
    if not file_rows:
        return

    # Scoring constants matching master_selection_service
    release_status_score: dict[str, int] = {"promotion": 100, "official": 0}
    release_type_score: dict[str, int] = {
        "album": 80, "ep": 70, "single": 60,
        "compilation": 40, "live": 30, "other": 20,
    }
    format_bonus: dict[str, int] = {"flac": 10, "aac": 6, "ogg": 6, "mp3": 3}

    def _score(row: dict[str, Any]) -> tuple[int, int, int]:
        score = 0
        rs = row.get("release_status")
        if rs:
            score += release_status_score.get(rs, 0)
        rt = row.get("release_type")
        if rt:
            score += release_type_score.get(rt, release_type_score["other"])
        fmt = (row.get("format") or "").lower()
        score += format_bonus.get(fmt, 1)
        return score, row.get("bitrate") or 0, row.get("duration_ms") or 0

    best = max(file_rows, key=_score)
    score_val, _, _ = _score(best)

    existing_id: UUID | None = None
    if sm_row is not None:
        id_cur = await conn.execute(
            "SELECT id FROM song_masters WHERE work_id = %s", (work_id,)
        )
        id_row = await id_cur.fetchone()
        if id_row:
            existing_id = id_row["id"]

    master_id = existing_id if existing_id is not None else uuid4()
    await conn.execute(
        """
        INSERT INTO song_masters
            (id, work_id, preferred_file_id, selection_method, score, updated_at)
        VALUES (%s, %s, %s, 'auto', %s, now())
        ON CONFLICT (work_id) DO UPDATE SET
            preferred_file_id = EXCLUDED.preferred_file_id,
            selection_method  = 'auto',
            score             = EXCLUDED.score,
            updated_at        = now()
        """,
        (master_id, work_id, best["id"], score_val),
    )


@router.post("/works/{target_id}/merge", response_model=MergeResponse)
async def merge_works(
    target_id: str, body: MergeRequest, conn: DbConn, _token: Token
) -> MergeResponse:
    """Merge one or more source works into a target work.

    Moves all files, format overrides, and recordings from source works into
    the target work, then deletes the now-empty source works. Recalculates
    the song master for the target work afterward.

    Args:
        target_id: The work to merge into.
        body: Contains ``source_work_ids`` list.
        conn: Async database connection.
        _token: Auth token.

    Returns:
        :class:`MergeResponse` with counts of moved files, deleted works, and dropped overrides.

    Raises:
        HTTPException: 404 if the target work does not exist.
        HTTPException: 422 if target_id appears in source_work_ids.
    """
    # Verify target exists
    target_cur = await conn.execute(
        "SELECT id, artist_id FROM works WHERE id = %s", (target_id,)
    )
    target_row = await target_cur.fetchone()
    if target_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Work {target_id} not found",
        )

    # Validate target not in sources
    if target_id in body.source_work_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Target work ID must not appear in source_work_ids",
        )

    if not body.source_work_ids:
        return MergeResponse(merged_file_count=0, deleted_work_count=0, dropped_override_count=0)

    # Filter to existing sources (idempotency — missing sources are no-op)
    placeholders = ", ".join("%s" for _ in body.source_work_ids)
    exist_cur = await conn.execute(
        f"SELECT id, artist_id FROM works WHERE id IN ({placeholders})",
        body.source_work_ids,
    )
    existing_source_rows = await exist_cur.fetchall()
    existing_source_ids = [r["id"] for r in existing_source_rows]

    if not existing_source_ids:
        return MergeResponse(merged_file_count=0, deleted_work_count=0, dropped_override_count=0)

    merged_file_count = 0
    dropped_override_count = 0

    src_placeholders = ", ".join("%s" for _ in existing_source_ids)

    # Move library_files (work_id column)
    files_cur = await conn.execute(
        f"UPDATE library_files SET work_id = %s WHERE work_id IN ({src_placeholders})",
        [target_id, *existing_source_ids],
    )
    merged_file_count = files_cur.rowcount if files_cur.rowcount is not None else 0

    # Move format_overrides — drop conflicts first (unique constraint on work_id, format_name)
    conflict_cur = await conn.execute(
        f"""
        SELECT fo_src.id AS src_id
        FROM format_overrides fo_src
        JOIN format_overrides fo_tgt
          ON fo_tgt.work_id = %s AND fo_tgt.format_name = fo_src.format_name
        WHERE fo_src.work_id IN ({src_placeholders})
        """,
        [target_id, *existing_source_ids],
    )
    conflict_rows = await conflict_cur.fetchall()
    dropped_override_count = len(conflict_rows)

    if conflict_rows:
        conflict_ids = [r["src_id"] for r in conflict_rows]
        conf_placeholders = ", ".join("%s" for _ in conflict_ids)
        await conn.execute(
            f"DELETE FROM format_overrides WHERE id IN ({conf_placeholders})",
            conflict_ids,
        )

    # Move non-conflicting overrides to target
    await conn.execute(
        f"UPDATE format_overrides SET work_id = %s WHERE work_id IN ({src_placeholders})",
        [target_id, *existing_source_ids],
    )

    # Move recordings to target work
    await conn.execute(
        f"UPDATE recordings SET work_id = %s WHERE work_id IN ({src_placeholders})",
        [target_id, *existing_source_ids],
    )

    # Re-link matches scoped to WORK target type
    await conn.execute(
        f"""
        UPDATE matches
        SET target_id = %s
        WHERE target_type = 'Work' AND target_id IN ({src_placeholders})
        """,
        [target_id, *existing_source_ids],
    )

    # Delete song_masters for source works, then delete source works
    await conn.execute(
        f"DELETE FROM song_masters WHERE work_id IN ({src_placeholders})",
        existing_source_ids,
    )
    deleted_cur = await conn.execute(
        f"DELETE FROM works WHERE id IN ({src_placeholders}) RETURNING id",
        existing_source_ids,
    )
    deleted_rows = await deleted_cur.fetchall()
    deleted_work_count = len(deleted_rows)

    # Recalculate song master for target
    await _recalculate_song_master(conn, target_id)

    # Orphan cleanup: delete local artists with no remaining works
    source_artist_ids = list({r["artist_id"] for r in existing_source_rows})
    for artist_id in source_artist_ids:
        if artist_id == target_row["artist_id"]:
            continue
        artist_cur = await conn.execute(
            "SELECT origin FROM artists WHERE id = %s", (artist_id,)
        )
        artist_row = await artist_cur.fetchone()
        if artist_row is None:
            continue
        if artist_row["origin"] != Origin.LOCAL:
            continue
        works_cur = await conn.execute(
            "SELECT id FROM works WHERE artist_id = %s LIMIT 1", (artist_id,)
        )
        if await works_cur.fetchone() is None:
            await conn.execute("DELETE FROM artists WHERE id = %s", (artist_id,))

    return MergeResponse(
        merged_file_count=merged_file_count,
        deleted_work_count=deleted_work_count,
        dropped_override_count=dropped_override_count,
    )


@router.post(
    "/works/{work_id}/split",
    response_model=SplitResponse,
    status_code=status.HTTP_201_CREATED,
)
async def split_work(
    work_id: str, body: SplitRequest, conn: DbConn, _token: Token
) -> SplitResponse:
    """Split a single file out of a work into its own new work.

    Creates a new local work (same artist and title), moves the file's recording
    to the new work, creates a song master for the new work, and deletes the old
    work if it becomes empty.

    Args:
        work_id: The source work containing the file.
        body: Contains ``file_id`` to split out.
        conn: Async database connection.
        _token: Auth token.

    Returns:
        :class:`SplitResponse` with new work ID and whether the old work was deleted.

    Raises:
        HTTPException: 404 if the work does not exist.
        HTTPException: 422 if the file does not belong to this work.
    """
    # Verify work exists
    work_cur = await conn.execute(
        "SELECT id, title, artist_id FROM works WHERE id = %s", (work_id,)
    )
    work_row = await work_cur.fetchone()
    if work_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Work {work_id} not found",
        )

    # Verify file exists and belongs to this work (via recording chain)
    file_cur = await conn.execute(
        """
        SELECT lf.id, lf.recording_id
        FROM library_files lf
        JOIN recordings r ON lf.recording_id = r.id
        WHERE lf.id = %s AND r.work_id = %s
        """,
        (body.file_id, work_id),
    )
    file_row = await file_cur.fetchone()
    if file_row is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"File {body.file_id} does not belong to work {work_id}",
        )

    recording_id = file_row["recording_id"]

    # Create new local work
    new_work_id = str(uuid4())
    await conn.execute(
        """
        INSERT INTO works (id, title, artist_id, origin, needs_enhancement)
        VALUES (%s, %s, %s, 'local', FALSE)
        """,
        (new_work_id, work_row["title"], work_row["artist_id"]),
    )

    # Move the recording to the new work
    await conn.execute(
        "UPDATE recordings SET work_id = %s WHERE id = %s",
        (new_work_id, recording_id),
    )

    # Update work_id on the file itself (denormalised column)
    await conn.execute(
        "UPDATE library_files SET work_id = %s WHERE id = %s",
        (new_work_id, body.file_id),
    )

    # Create song master for new work
    new_master_id = uuid4()
    await conn.execute(
        """
        INSERT INTO song_masters
            (id, work_id, preferred_file_id, selection_method, score, updated_at)
        VALUES (%s, %s, %s, 'auto', NULL, now())
        """,
        (new_master_id, new_work_id, body.file_id),
    )

    # Delete old work if now empty (no files remaining)
    count_cur = await conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM library_files lf
        JOIN recordings r ON lf.recording_id = r.id
        WHERE r.work_id = %s
        """,
        (work_id,),
    )
    count_row = await count_cur.fetchone()
    old_work_deleted = False
    if count_row and count_row["cnt"] == 0:
        await conn.execute("DELETE FROM song_masters WHERE work_id = %s", (work_id,))
        await conn.execute("DELETE FROM works WHERE id = %s", (work_id,))
        old_work_deleted = True
    else:
        # Recalculate master for the old work (it lost a file)
        await _recalculate_song_master(conn, work_id)

    return SplitResponse(new_work_id=new_work_id, old_work_deleted=old_work_deleted)


@router.patch("/files/{file_id}/work", response_model=ReassignResponse)
async def reassign_file_work(
    file_id: UUID, body: ReassignRequest, conn: DbConn, _token: Token
) -> ReassignResponse:
    """Reassign a library file to a different work.

    Moves the file (and its recording) to the target work, then cleans up the
    old work if it becomes empty.

    Args:
        file_id: The UUID of the file to reassign.
        body: Contains ``work_id`` of the target work.
        conn: Async database connection.
        _token: Auth token.

    Returns:
        :class:`ReassignResponse` with the old work ID and whether it was deleted.

    Raises:
        HTTPException: 404 if the file or target work does not exist.
        HTTPException: 422 if the file is already assigned to the target work.
    """
    # Verify file exists and get current work
    file_cur = await conn.execute(
        """
        SELECT lf.id, lf.recording_id, r.work_id AS current_work_id
        FROM library_files lf
        LEFT JOIN recordings r ON lf.recording_id = r.id
        WHERE lf.id = %s
        """,
        (file_id,),
    )
    file_row = await file_cur.fetchone()
    if file_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File {file_id} not found",
        )

    current_work_id: str | None = file_row["current_work_id"]
    recording_id: str | None = file_row["recording_id"]

    # Verify target work exists
    target_cur = await conn.execute(
        "SELECT id FROM works WHERE id = %s", (body.work_id,)
    )
    if await target_cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Work {body.work_id} not found",
        )

    # Verify file not already in target work
    if current_work_id == body.work_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"File {file_id} is already assigned to work {body.work_id}",
        )

    # Move recording to target work
    if recording_id is not None:
        await conn.execute(
            "UPDATE recordings SET work_id = %s WHERE id = %s",
            (body.work_id, recording_id),
        )

    # Update denormalised work_id on the file
    await conn.execute(
        "UPDATE library_files SET work_id = %s WHERE id = %s",
        (body.work_id, file_id),
    )

    # Recalculate master for the target work
    await _recalculate_song_master(conn, body.work_id)

    # Handle old work cleanup
    old_work_deleted = False
    if current_work_id is not None:
        count_cur = await conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM library_files lf
            JOIN recordings r ON lf.recording_id = r.id
            WHERE r.work_id = %s
            """,
            (current_work_id,),
        )
        count_row = await count_cur.fetchone()
        if count_row and count_row["cnt"] == 0:
            await conn.execute(
                "DELETE FROM song_masters WHERE work_id = %s", (current_work_id,)
            )
            await conn.execute("DELETE FROM works WHERE id = %s", (current_work_id,))
            old_work_deleted = True
        else:
            await _recalculate_song_master(conn, current_work_id)

    return ReassignResponse(old_work_id=current_work_id, old_work_deleted=old_work_deleted)
