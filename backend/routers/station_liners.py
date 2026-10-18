"""Station liners over HTTP (LR1-LR3, LR5): under ``/api/v1/stations/{station_id}/liners``,
``X-Airwave-Token`` required.

Errors map as plan note 15 says: an unknown station or another station's liner 404, a lost
duplicate race 409, too large 413, unsupported 415, unreadable or a bad value 422 in FastAPI's
list shape, a storage failure 503.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.dependencies import get_current_token, get_liner_ports
from backend.domain.broadcast import UnknownStationError
from backend.domain.liners import (
    MAX_LINER_BYTES,
    DuplicateLinerError,
    InvalidLinerValueError,
    LinerCueState,
    LinerEntry,
    LinerNotFoundError,
    LinerStorageError,
    LinerTooLargeError,
)
from backend.domain.streaming import (
    ClipFormat,
    ClipTooLargeError,
    UnreadableClipError,
    UnsupportedClipError,
)
from backend.domain.system import StorageUnavailableError
from backend.routers.clip_upload import (
    UploadAbortedError,
    UploadLimits,
    declared_over,
    read_audio_upload,
)
from backend.services.streaming.liners import (
    LinerLibrary,
    LinerPorts,
    UploadOutcome,
    add_liner,
    delete_liner,
    liner_audio,
    list_liners,
    rename_liner,
    set_liner_cadence,
)

router = APIRouter()

Token = Annotated[str, Depends(get_current_token)]
Ports = Annotated[LinerPorts, Depends(get_liner_ports)]

LINER_LIMITS = UploadLimits(
    max_file_bytes=MAX_LINER_BYTES,
    too_large=f"the upload is larger than a {MAX_LINER_BYTES // 2**20} MiB liner",
    name_of=lambda sent: sent,
    aborted_event="stream_liner_upload_aborted",
)
"""A liner upload's limits (LR2): the sent name is passed on as it is, with no folder part;
the liner store makes the display name of it (its stem, cut to length, or "liner")."""

_MEDIA_TYPES = {
    ClipFormat.FLAC: "audio/flac",
    ClipFormat.MP3: "audio/mpeg",
    ClipFormat.WAV: "audio/wav",
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LinerOut(BaseModel):
    """One liner, as the liners tab lists it."""

    id: UUID
    name: str
    format: ClipFormat
    duration_ms: int
    cues: LinerCueState
    uploaded_at: datetime


class LinerLibraryOut(BaseModel):
    """Response body for GET /liners and PUT /liners/cadence: the cadence and the liners."""

    songs_between_liners: int | None
    liners: list[LinerOut]


class CadenceIn(BaseModel):
    """Request body for PUT /liners/cadence: a strict whole number, or null for off (LR1).
    The range (1-1,000) is the domain's ``LinerCadence`` rule."""

    songs_between_liners: int | None = Field(strict=True)


class RenameIn(BaseModel):
    """Request body for PATCH /liners/{liner_id}: the new display name."""

    name: str = Field(strict=True)


class LinerAddedOut(BaseModel):
    """Response body for POST /liners: the liner's id, and whether it is new."""

    id: UUID
    outcome: UploadOutcome


def _liner_out(entry: LinerEntry) -> LinerOut:
    liner = entry.liner
    return LinerOut(
        id=liner.id,
        name=liner.name,
        format=liner.format,
        duration_ms=liner.duration_ms,
        cues=entry.cue_state,
        uploaded_at=liner.uploaded_at,
    )


def _library_out(library: LinerLibrary) -> LinerLibraryOut:
    cadence = library.cadence
    return LinerLibraryOut(
        songs_between_liners=None if cadence is None else cadence.songs,
        liners=[_liner_out(entry) for entry in library.entries],
    )


def _refuse_declared_too_large(request: Request) -> None:
    """413 from the ``Content-Length`` alone, before the body is read and before a database
    connection opens."""
    if declared_over(request, LINER_LIMITS):
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, LINER_LIMITS.too_large)


DeclaredSize = Annotated[None, Depends(_refuse_declared_too_large)]


def _invalid(field: str, refused: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[{"loc": ["body", field], "msg": str(refused), "type": "value_error"}],
    )


def _not_found(missing: Exception) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(missing))


def _storage_failed(failed: LinerStorageError) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(failed))


def _unavailable() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/{station_id}/liners", response_model=LinerLibraryOut)
def get_liners(station_id: UUID, _token: Token, ports: Ports) -> LinerLibraryOut:
    """The station's cadence and its liners, each with the state of its cues (LR1, LR5)."""
    try:
        library = list_liners(ports, station_id)
    except UnknownStationError as missing:
        raise _not_found(missing) from missing
    except StorageUnavailableError as lost:
        raise _unavailable() from lost
    return _library_out(library)


@router.put("/{station_id}/liners/cadence", response_model=LinerLibraryOut)
def put_liner_cadence(
    station_id: UUID, body: CadenceIn, _token: Token, ports: Ports
) -> LinerLibraryOut:
    """Store the station's cadence; null turns its liners off (LR1). Answers the list's
    full shape, so the tab has one response to read."""
    try:
        set_liner_cadence(ports, station_id, body.songs_between_liners)
        library = list_liners(ports, station_id)
    except UnknownStationError as missing:
        raise _not_found(missing) from missing
    except InvalidLinerValueError as refused:
        raise _invalid("songs_between_liners", refused) from refused
    except LinerStorageError as failed:
        raise _storage_failed(failed) from failed
    except StorageUnavailableError as lost:
        raise _unavailable() from lost
    return _library_out(library)


@router.post(
    "/{station_id}/liners", response_model=LinerAddedOut, status_code=status.HTTP_201_CREATED
)
async def post_liner(
    station_id: UUID, request: Request, _token: Token, _size: DeclaredSize, ports: Ports
) -> LinerAddedOut:
    """Store one uploaded file as a station liner, or find its audio already there (LR2).

    The token and the declared size are checked before ``ports`` opens a database connection
    (dependencies resolve in signature order). The body is read bounded by the liner limit;
    the liner is stored in the thread pool.
    """
    try:
        upload = await read_audio_upload(request, LINER_LIMITS)
        added = await run_in_threadpool(add_liner, ports, station_id, upload)
    except UploadAbortedError as aborted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(aborted)) from aborted
    except UnknownStationError as missing:
        raise _not_found(missing) from missing
    except DuplicateLinerError as raced:
        raise HTTPException(status.HTTP_409_CONFLICT, str(raced)) from raced
    except (ClipTooLargeError, LinerTooLargeError) as refused:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(refused)) from refused
    except UnsupportedClipError as refused:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(refused)) from refused
    except UnreadableClipError as refused:
        raise _invalid("file", refused) from refused
    except LinerStorageError as failed:
        raise _storage_failed(failed) from failed
    except StorageUnavailableError as lost:
        raise _unavailable() from lost
    return LinerAddedOut(id=added.liner_id, outcome=added.outcome)


@router.patch("/{station_id}/liners/{liner_id}", status_code=status.HTTP_204_NO_CONTENT)
def patch_liner(
    station_id: UUID, liner_id: UUID, body: RenameIn, _token: Token, ports: Ports
) -> Response:
    """Change only the liner's display name (LR3)."""
    try:
        rename_liner(ports, station_id, liner_id, body.name)
    except LinerNotFoundError as missing:
        raise _not_found(missing) from missing
    except InvalidLinerValueError as refused:
        raise _invalid("name", refused) from refused
    except LinerStorageError as failed:
        raise _storage_failed(failed) from failed
    except StorageUnavailableError as lost:
        raise _unavailable() from lost
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{station_id}/liners/{liner_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_liner_route(station_id: UUID, liner_id: UUID, _token: Token, ports: Ports) -> Response:
    """Remove the liner: its row, its file and its edit (LR3)."""
    try:
        delete_liner(ports, station_id, liner_id)
    except LinerNotFoundError as missing:
        raise _not_found(missing) from missing
    except LinerStorageError as failed:
        raise _storage_failed(failed) from failed
    except StorageUnavailableError as lost:
        raise _unavailable() from lost
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{station_id}/liners/{liner_id}/audio", response_class=FileResponse)
def get_liner_audio(station_id: UUID, liner_id: UUID, _token: Token, ports: Ports) -> FileResponse:
    """The liner's stored file, with its format's media type, for the tab's preview (LR5)."""
    try:
        audio = liner_audio(ports, station_id, liner_id)
    except LinerNotFoundError as missing:
        raise _not_found(missing) from missing
    except StorageUnavailableError as lost:
        raise _unavailable() from lost
    return FileResponse(audio.path, media_type=_MEDIA_TYPES[audio.format])
