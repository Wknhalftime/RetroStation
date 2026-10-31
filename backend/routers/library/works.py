from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import (
    SyncRepos,
    get_current_token,
    get_db_connection,
    get_master_choice_repos,
    get_work_curation_repos,
)
from backend.domain.curation import (
    CurationFileMissingError,
    CurationFileNotFoundError,
    CurationFileNotOnWorkError,
    CurationWorkNotFoundError,
    FileAlreadyOnWorkError,
    FormatOverrideExistsError,
    FormatOverrideNotFoundError,
    MasterFileMissingError,
    MasterFileNotOnWorkError,
    MasterWorkNotFoundError,
    MergeTargetInSourcesError,
    NewFormatOverride,
    SongMasterNotFoundError,
    WorkCurationError,
)
from backend.domain.enums import FileStatus
from backend.domain.synthetic_work_id import decode as decode_synthetic_work_id
from backend.services import work_curation_service as curation
from backend.services.master_choice_service import MasterChoiceRepos, choose_master
from backend.services.work_curation_service import WorkCurationRepos

router = APIRouter()

# The read routes' async connection; the writes run on SyncRepos.
DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
# Declared first in every write route: FastAPI resolves dependencies in declaration order, so
# a request without a token gets its 401 before a connection is opened.
Token = Annotated[str, Depends(get_current_token)]
ChoiceRepos = Annotated[MasterChoiceRepos, Depends(get_master_choice_repos)]
CurationRepos = Annotated[WorkCurationRepos, Depends(get_work_curation_repos)]


# ---------------------------------------------------------------------------
# Schemas — work detail
# ---------------------------------------------------------------------------


class OtherWorkRef(BaseModel):
    """The work a file is on, when a work's page shows it through a shared recording."""

    id: str
    title: str
    artist_name: str


class FileInfo(BaseModel):
    id: UUID
    file_path: str
    format: str
    bitrate: int | None
    duration_ms: int | None
    track_title: str | None
    release_title: str | None
    enrichment_status: str
    file_status: FileStatus
    other_work: OtherWorkRef | None = None


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


def _file_info(
    row: dict[str, Any], *, duration_ms: int | None, other_work: OtherWorkRef | None = None
) -> FileInfo:
    """Map a library_files row to FileInfo.

    ``duration_ms`` is resolved by the caller because the three call sites
    read it under different column aliases (``file_duration_ms`` when the
    row also carries a recording's own ``duration_ms``, ``duration_ms``
    otherwise); every other field is identical across all of them.
    """
    return FileInfo(
        id=row["file_id"],
        file_path=row["file_path"],
        format=row["format"],
        bitrate=row.get("bitrate"),
        duration_ms=duration_ms,
        track_title=row.get("track_title"),
        release_title=row.get("release_title"),
        enrichment_status=row["enrichment_status"],
        file_status=row["file_status"],
        other_work=other_work,
    )


def _other_work(row: dict[str, Any], work_id: str) -> OtherWorkRef | None:
    """The work a listed file is on, when that is not the work whose page lists it."""
    if row["file_work_id"] is None or row["file_work_id"] == work_id:
        return None
    return OtherWorkRef(
        id=row["file_work_id"],
        title=row["file_work_title"],
        artist_name=row["file_work_artist_name"],
    )


async def _work_recordings(
    conn: AsyncConnection[Any], work_id: str, work_title: str
) -> list[RecordingDetail]:
    """Return a real work's recordings, plus its orphan pseudo-recording if any."""
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
            lf.enrichment_status,
            lf.file_status,
            lf.work_id      AS file_work_id,
            fw.title        AS file_work_title,
            fa.name         AS file_work_artist_name
        FROM recordings r
        LEFT JOIN library_files lf ON lf.recording_id = r.id
        LEFT JOIN works fw ON fw.id = lf.work_id
        LEFT JOIN artists fa ON fa.id = fw.artist_id
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
                _file_info(
                    row,
                    duration_ms=row.get("file_duration_ms"),
                    other_work=_other_work(row, work_id),
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
            lf.enrichment_status,
            lf.file_status
        FROM library_files lf
        WHERE lf.work_id = %s AND lf.recording_id IS NULL
        ORDER BY lf.file_path
        """,
        (work_id,),
    )
    orphan_rows = await orphan_cur.fetchall()
    if orphan_rows:
        orphan_files = [_file_info(row, duration_ms=row.get("duration_ms")) for row in orphan_rows]
        recordings.append(
            RecordingDetail(
                id=f"orphan:{work_id}",
                title=work_title,
                version_type="original",
                duration_ms=None,
                files=orphan_files,
            )
        )

    return recordings


async def _work_song_master(conn: AsyncConnection[Any], work_id: str) -> SongMasterInfo | None:
    """Return the persisted song master for a real work, or None."""
    sm_cur = await conn.execute(
        "SELECT * FROM song_masters WHERE work_id = %s",
        (work_id,),
    )
    sm_row = await sm_cur.fetchone()
    if sm_row is None:
        return None
    return SongMasterInfo(
        id=sm_row["id"],
        preferred_file_id=sm_row["preferred_file_id"],
        selection_method=sm_row["selection_method"],
        score=sm_row.get("score"),
        updated_at=sm_row["updated_at"],
    )


async def _work_format_overrides(
    conn: AsyncConnection[Any], work_id: str
) -> list[FormatOverrideInfo]:
    """Return a real work's format overrides, ordered by format name."""
    fo_cur = await conn.execute(
        "SELECT * FROM format_overrides WHERE work_id = %s ORDER BY format_name",
        (work_id,),
    )
    fo_rows = await fo_cur.fetchall()
    return [
        FormatOverrideInfo(
            id=row["id"],
            format_name=row["format_name"],
            preferred_file_id=row["preferred_file_id"],
            notes=row.get("notes"),
            created_at=row["created_at"],
        )
        for row in fo_rows
    ]


async def _real_work_detail(conn: AsyncConnection[Any], work_id: str) -> WorkDetail:
    """Return WorkDetail for a real (non-synthetic) work id; raise 404 if missing."""
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

    recordings = await _work_recordings(conn, work_id, work_row["title"])
    song_master = await _work_song_master(conn, work_id)
    format_overrides = await _work_format_overrides(conn, work_id)

    return WorkDetail(
        id=work_row["id"],
        title=work_row["title"],
        artist_id=work_row["artist_id"],
        recordings=recordings,
        song_master=song_master,
        format_overrides=format_overrides,
    )


async def _synthetic_work_detail(
    conn: AsyncConnection[Any], work_id: str, artist_id: str, track_title: str
) -> WorkDetail:
    """Return WorkDetail for a synthetic id decoded into (artist_id, track_title).

    There is no work or recording row to back this: every matching
    library_files row becomes one file of a single synthesized recording.
    """
    file_cur = await conn.execute(
        """
        SELECT
            lf.id AS file_id, lf.file_path, lf.format, lf.bitrate,
            lf.duration_ms, lf.track_title, lf.release_title,
            lf.enrichment_status, lf.file_status, lf.recording_id
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

    files = [_file_info(row, duration_ms=row.get("duration_ms")) for row in file_rows]

    recordings = [
        RecordingDetail(
            id=work_id,
            title=track_title,
            version_type="original",
            duration_ms=file_rows[0].get("duration_ms") if file_rows else None,
            files=files,
        )
    ]

    return WorkDetail(
        id=work_id,
        title=track_title,
        artist_id=artist_id,
        recordings=recordings,
        song_master=None,
        format_overrides=[],
    )


# ---------------------------------------------------------------------------
# Routes — work detail
# ---------------------------------------------------------------------------


@router.get("/works/{work_id}", response_model=WorkDetail)
async def get_work_detail(work_id: str, conn: DbConn, _token: Token) -> WorkDetail:
    """Return a work with its recordings, files, master, and format overrides."""
    synthetic = decode_synthetic_work_id(work_id)
    if synthetic is None:
        return await _real_work_detail(conn, work_id)
    artist_id, track_title = synthetic
    return await _synthetic_work_detail(conn, work_id, artist_id, track_title)


# ---------------------------------------------------------------------------
# Routes — writes: sync routes on the request's one connection (SyncRepos, C75), each
# calling work_curation_service (or master_choice_service); a refusal's detail is the
# service error's message
# ---------------------------------------------------------------------------

_REFUSAL_STATUS: dict[type[WorkCurationError], int] = {
    CurationWorkNotFoundError: status.HTTP_404_NOT_FOUND,
    CurationFileNotFoundError: status.HTTP_404_NOT_FOUND,
    FormatOverrideNotFoundError: status.HTTP_404_NOT_FOUND,
    SongMasterNotFoundError: status.HTTP_404_NOT_FOUND,
    FormatOverrideExistsError: status.HTTP_409_CONFLICT,
    CurationFileNotOnWorkError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    CurationFileMissingError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    FileAlreadyOnWorkError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    MergeTargetInSourcesError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}
_REFUSALS = tuple(_REFUSAL_STATUS)


def _refused(error: WorkCurationError) -> HTTPException:
    """The refusal's status: its own class's, else the nearest mapped base class's."""
    code = next(_REFUSAL_STATUS[cls] for cls in type(error).__mro__ if cls in _REFUSAL_STATUS)
    return HTTPException(status_code=code, detail=str(error))


@router.put("/works/{work_id}/master", response_model=SongMasterResponse)
def set_work_master(
    _token: Token, work_id: str, body: SetMasterRequest, repos: ChoiceRepos
) -> SongMasterResponse:
    """Make a file the work's manual master (UPSERT).

    A file the work's page shows from another work (a shared recording) is moved onto
    this work first.
    """
    try:
        master = choose_master(work_id, body.preferred_file_id, repos)
    except MasterWorkNotFoundError as missing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(missing)) from missing
    except (MasterFileNotOnWorkError, MasterFileMissingError) as refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(refused)
        ) from refused
    return SongMasterResponse(
        id=master.id,
        work_id=master.work_id,
        preferred_file_id=master.preferred_file_id,
        selection_method=master.selection_method,
        score=master.score,
        updated_at=master.updated_at,
    )


@router.delete("/works/{work_id}/master", status_code=status.HTTP_204_NO_CONTENT)
def delete_work_master(_token: Token, work_id: str, repos: SyncRepos) -> None:
    """Remove the manual song master for a work."""
    try:
        curation.delete_master(work_id, repos.song_masters)
    except _REFUSALS as refused:
        raise _refused(refused) from refused


@router.post(
    "/works/{work_id}/format-overrides",
    response_model=FormatOverrideResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_format_override(
    _token: Token, work_id: str, body: CreateFormatOverrideRequest, repos: CurationRepos
) -> FormatOverrideResponse:
    """Create a per-format preferred file override for a work."""
    new = NewFormatOverride(
        format_name=body.format_name, preferred_file_id=body.preferred_file_id, notes=body.notes
    )
    try:
        stored = curation.create_format_override(work_id, new, repos)
    except _REFUSALS as refused:
        raise _refused(refused) from refused
    return FormatOverrideResponse(
        id=stored.id,
        work_id=stored.work_id,
        format_name=stored.format_name,
        preferred_file_id=stored.preferred_file_id,
        notes=stored.notes,
        created_at=stored.created_at,
    )


@router.delete(
    "/works/{work_id}/format-overrides/{override_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_format_override(
    _token: Token, work_id: str, override_id: UUID, repos: SyncRepos
) -> None:
    """Delete a format override by ID."""
    try:
        curation.delete_format_override(work_id, override_id, repos.format_overrides)
    except _REFUSALS as refused:
        raise _refused(refused) from refused


@router.post("/works/{target_id}/merge", response_model=MergeResponse)
def merge_works(
    _token: Token, target_id: str, body: MergeRequest, repos: CurationRepos
) -> MergeResponse:
    """Merge one or more source works into a target work."""
    try:
        merged = curation.merge_works(target_id, body.source_work_ids, repos)
    except _REFUSALS as refused:
        raise _refused(refused) from refused
    return MergeResponse(
        merged_file_count=merged.merged_file_count,
        deleted_work_count=merged.deleted_work_count,
        dropped_override_count=merged.dropped_override_count,
    )


@router.post(
    "/works/{work_id}/split",
    response_model=SplitResponse,
    status_code=status.HTTP_201_CREATED,
)
def split_work(
    _token: Token, work_id: str, body: SplitRequest, repos: CurationRepos
) -> SplitResponse:
    """Split a single file out of a work into its own new work."""
    try:
        split = curation.split_file_off(work_id, body.file_id, repos)
    except _REFUSALS as refused:
        raise _refused(refused) from refused
    return SplitResponse(new_work_id=split.new_work_id, old_work_deleted=split.old_work_deleted)


@router.patch("/files/{file_id}/work", response_model=ReassignResponse)
def reassign_file_work(
    _token: Token, file_id: UUID, body: ReassignRequest, repos: CurationRepos
) -> ReassignResponse:
    """Reassign a library file to a different work."""
    try:
        moved = curation.reassign_file(file_id, body.work_id, repos)
    except _REFUSALS as refused:
        raise _refused(refused) from refused
    return ReassignResponse(old_work_id=moved.old_work_id, old_work_deleted=moved.old_work_deleted)
