"""Streaming settings over HTTP (D10, D27, D34, D42; H1, H2): under ``/api/v1/streaming``,
``X-Airwave-Token`` required. ``POST``/``DELETE /sign-off`` land in PR G1's Task 3.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.dependencies import (
    get_current_token,
    get_sign_off_ports,
    get_streaming_state,
    get_user_settings,
)
from backend.domain.streaming import SignOff
from backend.domain.system import SettingsError
from backend.repositories.user_settings import UserSettingRepository
from backend.services.streaming.sign_off import SignOffPorts
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


def _sign_off_out(sign_off: SignOff | None) -> SignOffOut | None:
    if sign_off is None:
        return None
    return SignOffOut(name=sign_off.name, seconds=sign_off.span_ms / 1000, format=sign_off.format)


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
        sign_off=_sign_off_out(read.sign_off),
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
