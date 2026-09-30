"""The public stream (spec: "GET /listen/{call_letters}/{year}?key= returns the MP3 stream
through playout.relay", unauthenticated and outside /api/v1; Errors and edge cases; D14 plain
HTTP errors; D25 ICY titles; D6 a listener who leaves mid-tune-in leaves nothing running)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, status
from fastapi.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.dependencies import get_stream_service
from backend.domain.streaming import EndOfScheduleError, NoBroadcastError, StreamingError
from backend.services.streaming.errors import (
    InvalidStreamSettingError,
    StationBusyError,
    StationNotFoundError,
    StreamUnavailableError,
)
from backend.services.streaming.service import ListenRequest, StreamService

router = APIRouter()

_HTTP_ERRORS: dict[type[StreamingError], tuple[int, str]] = {
    StationNotFoundError: (status.HTTP_404_NOT_FOUND, "no broadcast"),
    NoBroadcastError: (status.HTTP_404_NOT_FOUND, "no broadcast"),
    EndOfScheduleError: (status.HTTP_404_NOT_FOUND, "no broadcast"),  # D43
    StationBusyError: (status.HTTP_503_SERVICE_UNAVAILABLE, "station busy"),
    StreamUnavailableError: (status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable"),
    InvalidStreamSettingError: (status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable"),  # D27
}
_OPEN_ERRORS = tuple(_HTTP_ERRORS)


class AsgiResponse(Response):
    """A response that hands the request to an ASGI app, which sends everything itself."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__()
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)


async def _silence(scope: Scope, receive: Receive, send: Send) -> None:
    """The answer to a client who has already left: nothing is sent."""


async def _departure(request: Request) -> None:
    """Returns once the client has disconnected."""
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def _unless_gone[T](request: Request, work: Coroutine[object, object, T]) -> T | None:
    """``work``'s result, or ``None`` when the client leaves first. ``work`` is then cancelled
    and has finished unwinding before this returns (D6: nothing is left running)."""
    working = asyncio.ensure_future(work)
    leaving = asyncio.ensure_future(_departure(request))
    try:
        await asyncio.wait({working, leaving}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        leaving.cancel()
        if not working.done():
            working.cancel()
            await asyncio.wait({working})
    if leaving.done() and not leaving.cancelled():
        leaving.exception()  # a failed watch counts as gone; retrieve its error, if any
    return None if working.cancelled() else working.result()


def _http_error(error: StreamingError) -> HTTPException:
    code, detail = next(answer for kind, answer in _HTTP_ERRORS.items() if isinstance(error, kind))
    return HTTPException(status_code=code, detail=detail)


@router.get("/listen/{call_letters}/{year}")
async def listen(
    request: Request,
    call_letters: str,
    year: Annotated[int, Path(ge=1, le=9999)],
    service: Annotated[StreamService, Depends(get_stream_service)],
    key: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    icy_metadata: Annotated[str | None, Header()] = None,
) -> Response:
    listener = ListenRequest(call_letters, year, key, icy_metadata)
    try:
        stream = await _unless_gone(request, service.open(listener))
    except _OPEN_ERRORS as error:
        raise _http_error(error) from error
    return AsgiResponse(_silence if stream is None else stream.app)
