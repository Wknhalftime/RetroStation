"""Streaming settings over HTTP (D10, D26, D27, D34, D42; H1, H2): under
``/api/v1/streaming``, ``X-Airwave-Token`` required.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from backend.dependencies import (
    get_current_token,
    get_sign_off_ports,
    get_streaming_state,
    get_user_settings,
)
from backend.domain.streaming import (
    ClipLengthError,
    ClipStorageError,
    ClipTooLargeError,
    SignOff,
    UnreadableClipError,
    UnsupportedClipError,
)
from backend.domain.system import SettingsError
from backend.repositories.user_settings import UserSettingRepository
from backend.routers.clip_upload import (
    TOO_LARGE,
    UploadAbortedError,
    declared_too_large,
    read_clip_upload,
)
from backend.services.streaming.sign_off import SignOffPorts, remove_sign_off, save_sign_off
from backend.services.streaming.stream_settings import (
    StreamingState,
    read_stream_settings,
    set_max_sessions,
)

router = APIRouter()

Token = Annotated[str, Depends(get_current_token)]
Settings = Annotated[UserSettingRepository, Depends(get_user_settings)]
State = Annotated[StreamingState, Depends(get_streaming_state)]
Ports = Annotated[SignOffPorts, Depends(get_sign_off_ports)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SignOffOut(BaseModel):
    """A sign-off clip, as the settings page shows it."""

    name: str
    seconds: float
    format: str


class StreamSettingsOut(BaseModel):
    """Response body for GET /settings: exactly the five keys the page reads."""

    streaming: StreamingState
    max_sessions: int | None
    max_sessions_problem: str | None
    sign_off: SignOffOut | None
    sign_off_problem: str | None


class MaxSessionsIn(BaseModel):
    """Request body for PUT /max-sessions: a strict whole number >= 1 (D27)."""

    value: int = Field(strict=True, ge=1)


class MaxSessionsOut(BaseModel):
    """Response body for PUT /max-sessions."""

    max_sessions: int


def _sign_off_out(sign_off: SignOff) -> SignOffOut:
    return SignOffOut(name=sign_off.name, seconds=sign_off.span_ms / 1000, format=sign_off.format)


def _refuse_declared_too_large(request: Request) -> None:
    """413 from the ``Content-Length`` alone, before the body is read (M3) and before a
    database connection opens (M5)."""
    if declared_too_large(request):
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, TOO_LARGE)


DeclaredSize = Annotated[None, Depends(_refuse_declared_too_large)]


def _clip_invalid(refused: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[{"loc": ["body", "file"], "msg": str(refused), "type": "value_error"}],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/settings", response_model=StreamSettingsOut)
def get_streaming_settings(
    settings: Settings, state: State, ports: Ports, _token: Token
) -> StreamSettingsOut:
    """The streaming settings page's five keys (D10, D27, D34; H2)."""
    read = read_stream_settings(settings, state, ports.folder)
    return StreamSettingsOut(
        streaming=read.streaming,
        max_sessions=read.max_sessions,
        max_sessions_problem=read.max_sessions_problem,
        sign_off=None if read.sign_off is None else _sign_off_out(read.sign_off),
        sign_off_problem=read.sign_off_problem,
    )


@router.put("/max-sessions", response_model=MaxSessionsOut)
def put_max_sessions(body: MaxSessionsIn, settings: Settings, _token: Token) -> MaxSessionsOut:
    """Validate and store the listener limit (D27; H2)."""
    try:
        stored = set_max_sessions(settings, body.value)
    except SettingsError as refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[{"loc": ["body", "value"], "msg": str(refused), "type": "value_error"}],
        ) from refused
    return MaxSessionsOut(max_sessions=stored)


@router.post("/sign-off", response_model=SignOffOut)
async def post_sign_off(
    request: Request, _token: Token, _size: DeclaredSize, ports: Ports
) -> SignOffOut:
    """Store the uploaded clip as the sign-off (D26; PG3, I2, I3, M3).

    The token and the declared size are checked before ``ports`` opens a database connection
    (dependencies resolve in signature order). The body is read bounded
    (``read_clip_upload``); the clip is stored in the thread pool.
    """
    try:
        upload = await read_clip_upload(request)
        stored = await run_in_threadpool(save_sign_off, ports, upload)
    except UploadAbortedError as aborted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(aborted)) from aborted
    except ClipTooLargeError as refused:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(refused)) from refused
    except UnsupportedClipError as refused:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(refused)) from refused
    except (UnreadableClipError, ClipLengthError) as refused:
        raise _clip_invalid(refused) from refused
    except ClipStorageError as failed:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(failed)) from failed
    return _sign_off_out(stored)


@router.delete("/sign-off", status_code=status.HTTP_204_NO_CONTENT)
def delete_sign_off(_token: Token, ports: Ports) -> Response:
    """Clear the sign-off clip (D26): the setting, then its file."""
    try:
        remove_sign_off(ports)
    except ClipStorageError as failed:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(failed)) from failed
    return Response(status_code=status.HTTP_204_NO_CONTENT)
