"""The internal session API a Liquidsoap engine calls (spec: the Backend <-> Liquidsoap
contract; D24, D45: open only to this machine's own clients, as the API is bound).

Contract: 200 is the item, 410 is the end of the schedule, and any other status tells the
engine to retry later (a seq that is not next yet answers 404, never 410).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status
from pydantic import BaseModel

from backend.dependencies import get_stream_service, require_internal_client
from backend.domain.streaming import EndOfScheduleError, StreamingError, StreamReadError
from backend.services.streaming.errors import (
    SessionTokenError,
    UnknownItemError,
    UnknownSessionError,
)
from backend.services.streaming.service import ItemCall, StreamService

router = APIRouter(
    prefix="/internal/stream/sessions/{session_id}",
    dependencies=[Depends(require_internal_client)],
    include_in_schema=False,
)

Service = Annotated[StreamService, Depends(get_stream_service)]
Seq = Annotated[int, Path(ge=0)]
SessionToken = Annotated[str | None, Header()]

_REPORT_ERRORS = (UnknownSessionError, UnknownItemError, SessionTokenError)
_ITEM_ERRORS = (*_REPORT_ERRORS, EndOfScheduleError, StreamReadError)
_HTTP_ERRORS: dict[type[StreamingError], tuple[int, str]] = {
    UnknownSessionError: (status.HTTP_404_NOT_FOUND, "unknown session"),
    UnknownItemError: (status.HTTP_404_NOT_FOUND, "unknown item"),
    SessionTokenError: (status.HTTP_403_FORBIDDEN, "wrong or missing session token"),
    EndOfScheduleError: (status.HTTP_410_GONE, "end of schedule"),
    StreamReadError: (status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable"),  # D88: a retry
}


class ItemBody(BaseModel):
    """One item, as the engine reads it: the file and its Liquidsoap annotations."""

    path: str
    annotations: dict[str, str]


def _http_error(error: StreamingError) -> HTTPException:
    code, detail = next(answer for kind, answer in _HTTP_ERRORS.items() if isinstance(error, kind))
    return HTTPException(status_code=code, detail=detail)


@router.get("/items/{seq}")
async def get_item(
    session_id: str, seq: Seq, service: Service, x_session_token: SessionToken = None
) -> ItemBody:
    try:
        payload = await service.item(ItemCall(session_id, x_session_token, seq))
    except _ITEM_ERRORS as error:
        raise _http_error(error) from error
    return ItemBody(path=payload.path, annotations=dict(payload.annotations))


@router.post("/items/{seq}/started", status_code=status.HTTP_204_NO_CONTENT)
async def item_started(
    session_id: str, seq: Seq, service: Service, x_session_token: SessionToken = None
) -> None:
    try:
        service.started(ItemCall(session_id, x_session_token, seq))
    except _REPORT_ERRORS as error:
        raise _http_error(error) from error


@router.post("/items/{seq}/failed", status_code=status.HTTP_204_NO_CONTENT)
async def item_failed(
    session_id: str, seq: Seq, service: Service, x_session_token: SessionToken = None
) -> None:
    try:
        service.failed(ItemCall(session_id, x_session_token, seq))
    except _REPORT_ERRORS as error:
        raise _http_error(error) from error
