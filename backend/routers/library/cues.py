"""The cue editor over HTTP (cue editor PR 2; R7): open a song's cues, save or reset a manual
edit, and serve the waveform and audio the editor draws. Under ``/api/v1/library``,
``X-Airwave-Token`` required.

Each route parses its input, calls one service function and maps the domain's errors (plan
note 11); the repositories are synchronous, so every route is a plain ``def``.
"""

from __future__ import annotations

import base64
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from starlette.responses import FileResponse

from backend.dependencies import (
    get_cue_editor_ports,
    get_current_token,
    get_peak_reader,
    get_stream_timing,
)
from backend.domain.streaming import (
    CueAudioUnreadableError,
    CueEditError,
    CueEditFileNotFoundError,
    CueEditRejectedError,
    CueFileNotEditableError,
    CueMarkers,
    InvalidStreamValueError,
    StreamTiming,
)
from backend.domain.system import StorageUnavailableError
from backend.services.streaming.cue_editor import (
    CueAnalysisState,
    CueEditorPorts,
    ReadPeaks,
    audio_path,
    read_cue_editor,
    read_waveform,
    reset_cue_edit,
    save_cue_edit,
)

router = APIRouter()

Token = Annotated[str, Depends(get_current_token)]
Ports = Annotated[CueEditorPorts, Depends(get_cue_editor_ports)]
PeakReader = Annotated[ReadPeaks, Depends(get_peak_reader)]
Timing = Annotated[StreamTiming, Depends(get_stream_timing)]

_STATUS_OF: dict[type[CueEditError], int] = {
    CueEditFileNotFoundError: status.HTTP_404_NOT_FOUND,
    CueFileNotEditableError: status.HTTP_409_CONFLICT,
    CueAudioUnreadableError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}
_MEDIA_TYPE_OF = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "aac": "audio/mp4",  # the scanner stores .m4a as "aac"
    "ogg": "audio/ogg",
}
_UNKNOWN_MEDIA_TYPE = "application/octet-stream"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class MarkersModel(BaseModel):
    """The four markers, whole milliseconds from the start of the file (strict, >= 0)."""

    start_ms: int = Field(strict=True, ge=0)
    eom_ms: int = Field(strict=True, ge=0)
    fade_at_ms: int = Field(strict=True, ge=0)
    stop_ms: int = Field(strict=True, ge=0)


class CueEditorOut(BaseModel):
    """Response body for GET /files/{file_id}/cues."""

    file_id: UUID
    title: str | None
    artist: str | None
    format: str
    duration_ms: int
    analysis: CueAnalysisState
    automatic: MarkersModel
    manual: MarkersModel | None


class WaveformOut(BaseModel):
    """Response body for GET /files/{file_id}/waveform: ``peaks`` is base64 of the bytes."""

    duration_ms: int
    per_second: int
    peaks: str


def _markers_out(markers: CueMarkers) -> MarkersModel:
    return MarkersModel(
        start_ms=markers.start_ms,
        eom_ms=markers.eom_ms,
        fade_at_ms=markers.fade_at_ms,
        stop_ms=markers.stop_ms,
    )


def _body_invalid(reason: str) -> HTTPException:
    """A 422 in FastAPI's list shape, located at the request body."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[{"loc": ["body"], "msg": reason, "type": "value_error"}],
    )


def _http_error(failure: CueEditError | StorageUnavailableError) -> HTTPException:
    """The HTTP answer for a domain failure (plan note 11)."""
    if isinstance(failure, StorageUnavailableError):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable")
    if isinstance(failure, CueEditRejectedError):
        return _body_invalid(str(failure))
    for kind in type(failure).__mro__:
        if kind in _STATUS_OF:
            return HTTPException(_STATUS_OF[kind], str(failure))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(failure))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/files/{file_id}/cues", response_model=CueEditorOut)
def get_cues(file_id: UUID, _token: Token, ports: Ports, timing: Timing) -> CueEditorOut:
    """The song's cues as the editor opens on them: what the radio plays, and any saved edit."""
    try:
        view = read_cue_editor(ports, file_id, timing)
    except (CueEditError, StorageUnavailableError) as failure:
        raise _http_error(failure) from failure
    return CueEditorOut(
        file_id=view.file_id,
        title=view.title,
        artist=view.artist,
        format=view.format,
        duration_ms=view.duration_ms,
        analysis=view.analysis,
        automatic=_markers_out(view.automatic),
        manual=None if view.manual is None else _markers_out(view.manual),
    )


@router.put("/files/{file_id}/cues", status_code=status.HTTP_204_NO_CONTENT)
def put_cues(
    file_id: UUID, body: MarkersModel, _token: Token, ports: Ports, timing: Timing
) -> Response:
    """Save a manual edit, unless the markers are out of order or the radio would refuse them."""
    try:
        markers = CueMarkers(**body.model_dump())
    except InvalidStreamValueError as disordered:
        raise _body_invalid(str(disordered)) from disordered
    try:
        save_cue_edit(ports, file_id, markers, timing)
    except (CueEditError, StorageUnavailableError) as failure:
        raise _http_error(failure) from failure
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/files/{file_id}/cues", status_code=status.HTTP_204_NO_CONTENT)
def delete_cues(file_id: UUID, _token: Token, ports: Ports) -> Response:
    """Remove the manual edit; a song without one is left as it is."""
    try:
        reset_cue_edit(ports, file_id)
    except (CueEditError, StorageUnavailableError) as failure:
        raise _http_error(failure) from failure
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/files/{file_id}/waveform", response_model=WaveformOut)
def get_waveform(file_id: UUID, _token: Token, ports: Ports, read: PeakReader) -> WaveformOut:
    """The song's peaks, for the editor to draw."""
    try:
        waveform = read_waveform(ports, file_id, read)
    except (CueEditError, StorageUnavailableError) as failure:
        raise _http_error(failure) from failure
    return WaveformOut(
        duration_ms=waveform.duration_ms,
        per_second=waveform.per_second,
        peaks=base64.b64encode(waveform.peaks).decode("ascii"),
    )


@router.get("/files/{file_id}/audio")
def get_audio(file_id: UUID, _token: Token, ports: Ports) -> FileResponse:
    """The song's file, for the editor to play."""
    try:
        source = audio_path(ports, file_id)
    except (CueEditError, StorageUnavailableError) as failure:
        raise _http_error(failure) from failure
    media_type = _MEDIA_TYPE_OF.get(source.format.lower(), _UNKNOWN_MEDIA_TYPE)
    return FileResponse(source.path, media_type=media_type)
