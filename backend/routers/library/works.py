from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import get_current_token, get_db_connection
from backend.domain.enums import CatalogSource, TargetType
from backend.domain.synthetic_work_id import decode as decode_synthetic_work_id

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


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
# Private helpers
# ---------------------------------------------------------------------------


async def _recalculate_song_master(conn: AsyncConnection[Any], work_id: str) -> None:
    """Recalculate the auto song master for a work using async SQL.

    Honors a manual master only if its preferred_file_id still belongs to the
    work; otherwise falls through to auto-recalculation so split_work and
    reassign_file_work cannot leave a stale pointer.
    """
    sm_cur = await conn.execute(
        "SELECT id, preferred_file_id, selection_method FROM song_masters WHERE work_id = %s",
        (work_id,),
    )
    sm_row = await sm_cur.fetchone()
    if sm_row is not None and sm_row["selection_method"] == "manual":
        member_cur = await conn.execute(
            """
            SELECT 1
            FROM library_files lf
            WHERE lf.id = %s AND lf.work_id = %s
            """,
            (sm_row["preferred_file_id"], work_id),
        )
        if await member_cur.fetchone() is not None:
            return

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
        WHERE lf.work_id = %s
        """,
        (work_id,),
    )
    file_rows = await files_cur.fetchall()
    if not file_rows:
        return

    release_status_score: dict[str, int] = {"promotion": 100, "official": 0}
    release_type_score: dict[str, int] = {
        "album": 80,
        "ep": 70,
        "single": 60,
        "compilation": 40,
        "live": 30,
        "other": 20,
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

    master_id = sm_row["id"] if sm_row is not None else uuid4()
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


async def _consolidate_recordings(
    conn: AsyncConnection[Any],
    source_work_ids: list[str],
    target_work_id: str,
) -> None:
    """Move recordings from source works to target, merging duplicates.

    When a source recording has the same version_type as an existing target
    recording, reassign files from the source recording to the target one
    and delete the duplicate source recording. Non-conflicting recordings
    are simply moved to the target work.
    """
    if not source_work_ids:
        return

    src_ph = ", ".join("%s" for _ in source_work_ids)

    src_cur = await conn.execute(
        f"SELECT id, version_type FROM recordings WHERE work_id IN ({src_ph})",
        source_work_ids,
    )
    src_recs = await src_cur.fetchall()

    for src_rec in src_recs:
        tgt_cur = await conn.execute(
            "SELECT id FROM recordings WHERE work_id = %s AND version_type = %s",
            (target_work_id, src_rec["version_type"]),
        )
        tgt_row = await tgt_cur.fetchone()

        if tgt_row:
            await conn.execute(
                "UPDATE library_files SET recording_id = %s WHERE recording_id = %s",
                (tgt_row["id"], src_rec["id"]),
            )
            await conn.execute(
                "DELETE FROM recordings WHERE id = %s",
                (src_rec["id"],),
            )
        else:
            await conn.execute(
                "UPDATE recordings SET work_id = %s WHERE id = %s",
                (target_work_id, src_rec["id"]),
            )


# ---------------------------------------------------------------------------
# Routes — work detail
# ---------------------------------------------------------------------------


@router.get("/works/{work_id}", response_model=WorkDetail)
async def get_work_detail(work_id: str, conn: DbConn, _token: Token) -> WorkDetail:
    """Return a work with its recordings, files, master, and format overrides."""
    synthetic = decode_synthetic_work_id(work_id)
    work_row: dict[str, Any] | None = None
    recordings: list[RecordingDetail] = []
    song_master: SongMasterInfo | None = None
    format_overrides: list[FormatOverrideInfo] = []

    if synthetic is None:
        work_cur = await conn.execute(
            "SELECT id, title, artist_id FROM works WHERE id = %s",
            (work_id,),
        )
        work_row = await work_cur.fetchone()
        if work_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Work {work_id} not found",
            )

    if work_row is not None:
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
            WHERE r.id IN (
                SELECT id FROM recordings WHERE work_id = %s
                UNION
                SELECT recording_id FROM library_files
                WHERE work_id = %s AND recording_id IS NOT NULL
            )
            ORDER BY r.id, lf.file_path
            """,
            (work_id, work_id),
        )
        rec_rows = await rec_cur.fetchall()

        recordings_map: dict[str, dict[str, Any]] = {}
        for row in rec_rows:
            rid = row["rec_id"]
            if rid not in recordings_map:
                recordings_map[rid] = {
                    "id": rid,
                    "title": row["rec_title"],
                    "version_type": row["rec_version_type"] or "original",
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

        orphan_cur = await conn.execute(
            """
            SELECT
                lf.id AS file_id,
                lf.file_path,
                lf.format,
                lf.bitrate,
                lf.duration_ms,
                lf.track_title,
                lf.release_title,
                lf.enrichment_status
            FROM library_files lf
            WHERE lf.work_id = %s AND lf.recording_id IS NULL
            ORDER BY lf.file_path
            """,
            (work_id,),
        )
        orphan_rows = await orphan_cur.fetchall()
        if orphan_rows:
            orphan_files = [
                FileInfo(
                    id=row["file_id"],
                    file_path=row["file_path"],
                    format=row["format"],
                    bitrate=row.get("bitrate"),
                    duration_ms=row.get("duration_ms"),
                    track_title=row.get("track_title"),
                    release_title=row.get("release_title"),
                    enrichment_status=row["enrichment_status"],
                )
                for row in orphan_rows
            ]
            recordings.append(
                RecordingDetail(
                    id=f"orphan:{work_id}",
                    title=work_row["title"],
                    version_type="original",
                    duration_ms=None,
                    files=orphan_files,
                )
            )

        sm_cur = await conn.execute(
            "SELECT * FROM song_masters WHERE work_id = %s",
            (work_id,),
        )
        sm_row = await sm_cur.fetchone()
        if sm_row is not None:
            song_master = SongMasterInfo(
                id=sm_row["id"],
                preferred_file_id=sm_row["preferred_file_id"],
                selection_method=sm_row["selection_method"],
                score=sm_row.get("score"),
                updated_at=sm_row["updated_at"],
            )

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
    else:
        assert synthetic is not None
        artist_id, track_title = synthetic

        file_cur = await conn.execute(
            """
            SELECT
                lf.id AS file_id, lf.file_path, lf.format, lf.bitrate,
                lf.duration_ms, lf.track_title, lf.release_title,
                lf.enrichment_status, lf.recording_id
            FROM library_files lf
            WHERE (lf.album_artist_mbid = %s OR lf.artist_mbid = %s)
              AND lf.track_title = %s
            ORDER BY lf.file_path
            """,
            (artist_id, artist_id, track_title),
        )
        file_rows = await file_cur.fetchall()

        if not file_rows:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Work {work_id} not found",
            )

        files = [
            FileInfo(
                id=row["file_id"],
                file_path=row["file_path"],
                format=row["format"],
                bitrate=row.get("bitrate"),
                duration_ms=row.get("duration_ms"),
                track_title=row.get("track_title"),
                release_title=row.get("release_title"),
                enrichment_status=row["enrichment_status"],
            )
            for row in file_rows
        ]

        recordings = [
            RecordingDetail(
                id=work_id,
                title=track_title,
                version_type="original",
                duration_ms=file_rows[0].get("duration_ms") if file_rows else None,
                files=files,
            )
        ]

        work_row = {
            "id": work_id,
            "title": track_title,
            "artist_id": artist_id,
        }

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
    """Manually set the preferred file for a work (UPSERT)."""
    work_cur = await conn.execute("SELECT id FROM works WHERE id = %s", (work_id,))
    if await work_cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Work {work_id} not found",
        )

    file_cur = await conn.execute(
        "SELECT 1 FROM library_files WHERE id = %s AND work_id = %s",
        (body.preferred_file_id, work_id),
    )
    if await file_cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"File {body.preferred_file_id} does not belong to work {work_id}",
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

    row_cur = await conn.execute("SELECT * FROM song_masters WHERE work_id = %s", (work_id,))
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
async def delete_work_master(work_id: str, conn: DbConn, _token: Token) -> None:
    """Remove the manual song master for a work."""
    cur = await conn.execute("SELECT id FROM song_masters WHERE work_id = %s", (work_id,))
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
    """Create a per-format preferred file override for a work."""
    work_cur = await conn.execute("SELECT id FROM works WHERE id = %s", (work_id,))
    if await work_cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Work {work_id} not found",
        )

    file_cur = await conn.execute(
        "SELECT 1 FROM library_files WHERE id = %s AND work_id = %s",
        (body.preferred_file_id, work_id),
    )
    if await file_cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"File {body.preferred_file_id} does not belong to work {work_id}",
        )

    override_id = uuid4()
    insert_cur = await conn.execute(
        """
        INSERT INTO format_overrides (id, work_id, format_name, preferred_file_id, notes)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (work_id, format_name) DO NOTHING
        RETURNING id
        """,
        (override_id, work_id, body.format_name, body.preferred_file_id, body.notes),
    )
    if await insert_cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Format override for work {work_id} and format "
                f"'{body.format_name}' already exists"
            ),
        )

    row_cur = await conn.execute("SELECT * FROM format_overrides WHERE id = %s", (override_id,))
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
    """Delete a format override by ID."""
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
# Routes — merge / split / reassign
# ---------------------------------------------------------------------------


@router.post("/works/{target_id}/merge", response_model=MergeResponse)
async def merge_works(
    target_id: str, body: MergeRequest, conn: DbConn, _token: Token
) -> MergeResponse:
    """Merge one or more source works into a target work."""
    target_cur = await conn.execute("SELECT id, artist_id FROM works WHERE id = %s", (target_id,))
    target_row = await target_cur.fetchone()
    if target_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Work {target_id} not found",
        )

    if target_id in body.source_work_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Target work ID must not appear in source_work_ids",
        )

    if not body.source_work_ids:
        return MergeResponse(merged_file_count=0, deleted_work_count=0, dropped_override_count=0)

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

    files_cur = await conn.execute(
        f"UPDATE library_files SET work_id = %s WHERE work_id IN ({src_placeholders})",
        [target_id, *existing_source_ids],
    )
    merged_file_count = files_cur.rowcount if files_cur.rowcount is not None else 0

    # Drop source overrides that collide with an existing target override
    # (target wins) OR collide with another source override for the same
    # format_name (oldest source wins, id tie-break) so the subsequent bulk
    # UPDATE cannot violate the UNIQUE (work_id, format_name) constraint.
    drop_cur = await conn.execute(
        f"""
        SELECT id FROM format_overrides
        WHERE work_id IN ({src_placeholders})
          AND (
            format_name IN (
              SELECT format_name FROM format_overrides WHERE work_id = %s
            )
            OR id NOT IN (
              SELECT DISTINCT ON (format_name) id
              FROM format_overrides
              WHERE work_id IN ({src_placeholders})
                AND format_name NOT IN (
                  SELECT format_name FROM format_overrides WHERE work_id = %s
                )
              ORDER BY format_name, created_at ASC, id ASC
            )
          )
        """,
        [*existing_source_ids, target_id, *existing_source_ids, target_id],
    )
    drop_rows = await drop_cur.fetchall()
    dropped_override_count = len(drop_rows)

    if drop_rows:
        drop_ids = [r["id"] for r in drop_rows]
        drop_placeholders = ", ".join("%s" for _ in drop_ids)
        await conn.execute(
            f"DELETE FROM format_overrides WHERE id IN ({drop_placeholders})",
            drop_ids,
        )

    await conn.execute(
        f"UPDATE format_overrides SET work_id = %s WHERE work_id IN ({src_placeholders})",
        [target_id, *existing_source_ids],
    )

    await _consolidate_recordings(conn, existing_source_ids, target_id)

    await conn.execute(
        f"""
        UPDATE matches
        SET target_id = %s
        WHERE target_type = %s AND target_id IN ({src_placeholders})
        """,
        [target_id, TargetType.WORK.value, *existing_source_ids],
    )

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

    await _recalculate_song_master(conn, target_id)

    source_artist_ids = list({r["artist_id"] for r in existing_source_rows})
    for artist_id in source_artist_ids:
        if artist_id == target_row["artist_id"]:
            continue
        artist_cur = await conn.execute("SELECT origin FROM artists WHERE id = %s", (artist_id,))
        artist_row = await artist_cur.fetchone()
        if artist_row is None:
            continue
        if artist_row["origin"] != CatalogSource.LOCAL:
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
    """Split a single file out of a work into its own new work."""
    work_cur = await conn.execute(
        "SELECT id, title, artist_id FROM works WHERE id = %s", (work_id,)
    )
    work_row = await work_cur.fetchone()
    if work_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Work {work_id} not found",
        )

    file_cur = await conn.execute(
        """
        SELECT lf.id, lf.recording_id
        FROM library_files lf
        WHERE lf.id = %s AND lf.work_id = %s
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

    new_work_id = str(uuid4())
    await conn.execute(
        """
        INSERT INTO works (id, title, artist_id, origin, needs_enhancement)
        VALUES (%s, %s, %s, 'local', FALSE)
        """,
        (new_work_id, work_row["title"], work_row["artist_id"]),
    )

    if recording_id is not None:
        rec_cur = await conn.execute(
            "SELECT title, version_type, duration_ms FROM recordings WHERE id = %s",
            (recording_id,),
        )
        rec_row = await rec_cur.fetchone()
        new_rec_id = str(uuid4())
        await conn.execute(
            """INSERT INTO recordings
                   (id, title, work_id, version_type, duration_ms,
                    needs_enhancement)
               VALUES (%s, %s, %s, %s, %s, FALSE)""",
            (
                new_rec_id,
                rec_row["title"] if rec_row else work_row["title"],
                new_work_id,
                rec_row["version_type"] if rec_row else "original",
                rec_row["duration_ms"] if rec_row else None,
            ),
        )
        await conn.execute(
            "UPDATE library_files SET recording_id = %s WHERE id = %s",
            (new_rec_id, body.file_id),
        )

    await conn.execute(
        "UPDATE library_files SET work_id = %s WHERE id = %s",
        (new_work_id, body.file_id),
    )

    new_master_id = uuid4()
    await conn.execute(
        """
        INSERT INTO song_masters
            (id, work_id, preferred_file_id, selection_method, score, updated_at)
        VALUES (%s, %s, %s, 'auto', NULL, now())
        """,
        (new_master_id, new_work_id, body.file_id),
    )

    count_cur = await conn.execute(
        "SELECT COUNT(*) AS cnt FROM library_files WHERE work_id = %s",
        (work_id,),
    )
    count_row = await count_cur.fetchone()
    old_work_deleted = False
    if count_row and count_row["cnt"] == 0:
        await conn.execute("DELETE FROM song_masters WHERE work_id = %s", (work_id,))
        await conn.execute("DELETE FROM works WHERE id = %s", (work_id,))
        old_work_deleted = True
    else:
        await _recalculate_song_master(conn, work_id)

    return SplitResponse(new_work_id=new_work_id, old_work_deleted=old_work_deleted)


@router.patch("/files/{file_id}/work", response_model=ReassignResponse)
async def reassign_file_work(
    file_id: UUID, body: ReassignRequest, conn: DbConn, _token: Token
) -> ReassignResponse:
    """Reassign a library file to a different work."""
    file_cur = await conn.execute(
        """
        SELECT lf.id, lf.recording_id, lf.work_id AS current_work_id
        FROM library_files lf
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

    target_cur = await conn.execute("SELECT id FROM works WHERE id = %s", (body.work_id,))
    if await target_cur.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Work {body.work_id} not found",
        )

    if current_work_id == body.work_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"File {file_id} is already assigned to work {body.work_id}",
        )

    if recording_id is not None:
        share_cur = await conn.execute(
            "SELECT COUNT(*) AS cnt FROM library_files WHERE recording_id = %s AND id != %s",
            (recording_id, str(file_id)),
        )
        share_row = await share_cur.fetchone()
        is_shared = share_row is not None and share_row["cnt"] > 0

        rec_cur = await conn.execute(
            "SELECT title, version_type, duration_ms FROM recordings WHERE id = %s",
            (recording_id,),
        )
        rec_row = await rec_cur.fetchone()
        rec_version = rec_row["version_type"] if rec_row else "original"

        tgt_cur = await conn.execute(
            "SELECT id FROM recordings WHERE work_id = %s AND version_type = %s",
            (body.work_id, rec_version),
        )
        tgt_rec = await tgt_cur.fetchone()

        if tgt_rec:
            await conn.execute(
                "UPDATE library_files SET recording_id = %s WHERE id = %s",
                (tgt_rec["id"], str(file_id)),
            )
        elif is_shared:
            new_rec_id = str(uuid4())
            await conn.execute(
                """INSERT INTO recordings
                       (id, title, work_id, version_type, duration_ms,
                        needs_enhancement)
                   VALUES (%s, %s, %s, %s, %s, FALSE)""",
                (
                    new_rec_id,
                    rec_row["title"] if rec_row else "",
                    body.work_id,
                    rec_version,
                    rec_row["duration_ms"] if rec_row else None,
                ),
            )
            await conn.execute(
                "UPDATE library_files SET recording_id = %s WHERE id = %s",
                (new_rec_id, str(file_id)),
            )
        else:
            await conn.execute(
                "UPDATE recordings SET work_id = %s WHERE id = %s",
                (body.work_id, recording_id),
            )

    await conn.execute(
        "UPDATE library_files SET work_id = %s WHERE id = %s",
        (body.work_id, file_id),
    )

    await _recalculate_song_master(conn, body.work_id)

    old_work_deleted = False
    if current_work_id is not None:
        count_cur = await conn.execute(
            "SELECT COUNT(*) AS cnt FROM library_files WHERE work_id = %s",
            (current_work_id,),
        )
        count_row = await count_cur.fetchone()
        if count_row and count_row["cnt"] == 0:
            await conn.execute("DELETE FROM song_masters WHERE work_id = %s", (current_work_id,))
            await conn.execute("DELETE FROM works WHERE id = %s", (current_work_id,))
            old_work_deleted = True
        else:
            await _recalculate_song_master(conn, current_work_id)

    return ReassignResponse(old_work_id=current_work_id, old_work_deleted=old_work_deleted)
