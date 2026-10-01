"""Drive a server-sent-event route by calling the ASGI app directly (D13; X1).

``httpx.ASGITransport`` buffers the whole body, so a response that never ends would hang it.
``SseDrive`` instead runs the app as a task, sees each frame as it is sent, and decides when
the client leaves (``http.disconnect``). The scope follows
``tests/playout/test_listen_give_up.listen_scope``, with ASGI ``spec_version`` 2.3: below 2.4,
Starlette listens for ``http.disconnect`` while it streams. Every wait has a 2 s hang guard
that fails the test; none is used for ordering.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import MutableMapping
from dataclasses import dataclass
from types import TracebackType

from fastapi import FastAPI

__all__ = ["HANG_GUARD_S", "SseDrive", "SseResult"]

HANG_GUARD_S = 2.0

type Frame = tuple[str, dict[str, str]]
"""One event: its ``event:`` name and its ``data:`` decoded from JSON."""


@dataclass(frozen=True)
class SseResult:
    """How the response ended: its status, headers, the events it carried, and the JSON body
    of a non-SSE answer (``None`` for an event stream)."""

    status: int
    headers: dict[str, str]
    events: list[Frame]
    json: object | None


def _scope(path: str, query: str) -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"accept", b"text/event-stream")],
        "client": ("192.168.1.30", 50123),
        "server": ("127.0.0.1", 8010),
    }


def _frame(block: str) -> Frame | None:
    """One SSE block as (event, data); ``None`` for a comment-only block (a keep-alive)."""
    event, data = "message", ""
    for line in block.split("\n"):
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data += line.removeprefix("data:").strip()
    if not data:
        return None
    decoded = json.loads(data)
    if not isinstance(decoded, dict):
        raise AssertionError(f"an event's data is not a JSON object: {data!r}")
    return event, {str(k): str(v) for k, v in decoded.items()}


class SseDrive:
    """One client of ``path?query`` on ``app``: started at construction, left on ``leave()``.

    Use it as an async context manager so the client always leaves and the app task ends.
    """

    def __init__(self, app: FastAPI, path: str, query: str) -> None:
        self.started = asyncio.Event()
        self._status = 0
        self._headers: dict[str, str] = {}
        self._body = b""
        self._events: list[Frame] = []
        self._new_frame = asyncio.Event()
        self._requested = False
        self._left = asyncio.Event()
        self._task = asyncio.create_task(app(_scope(path, query), self._receive, self._send))

    async def _receive(self) -> dict[str, object]:
        if not self._requested:
            self._requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._left.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: MutableMapping[str, object]) -> None:
        if message["type"] == "http.response.start":
            status = message["status"]
            raw = message.get("headers", [])
            assert isinstance(status, int) and isinstance(raw, list)
            self._status = status
            self._headers = {bytes(k).decode().lower(): bytes(v).decode() for k, v in raw}
            self.started.set()
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            assert isinstance(chunk, bytes)
            self._body += chunk
            self._parse()

    def _parse(self) -> None:
        if not self._headers.get("content-type", "").startswith("text/event-stream"):
            return
        text = self._body.decode().replace("\r\n", "\n")
        blocks = text.split("\n\n")[:-1]  # the last piece is an unfinished block
        frames = [f for f in (_frame(b) for b in blocks) if f is not None]
        if len(frames) > len(self._events):
            self._events = frames
            self._new_frame.set()

    async def wait_started(self) -> None:
        """Wait until the response has started (hang guard)."""
        try:
            await asyncio.wait_for(self.started.wait(), HANG_GUARD_S)
        except TimeoutError as hung:
            raise AssertionError(f"the response did not start within {HANG_GUARD_S} s") from hung

    def leave(self) -> None:
        """The client disconnects."""
        self._left.set()

    async def frames(self, count: int) -> list[Frame]:
        """The first ``count`` events, once they have been sent (hang guard)."""

        async def enough() -> None:
            while len(self._events) < count and not self._task.done():
                self._new_frame.clear()
                waiting = asyncio.ensure_future(self._new_frame.wait())
                await asyncio.wait({waiting, self._task}, return_when=asyncio.FIRST_COMPLETED)
                waiting.cancel()

        await asyncio.wait_for(enough(), HANG_GUARD_S)
        if len(self._events) < count:
            raise AssertionError(f"the response ended after {self._events}")
        return self._events[:count]

    async def result(self) -> SseResult:
        """The response, once the app has finished with it (hang guard)."""
        await asyncio.wait_for(asyncio.shield(self._task), HANG_GUARD_S)
        is_json = self._headers.get("content-type", "").startswith("application/json")
        return SseResult(
            status=self._status,
            headers=dict(self._headers),
            events=list(self._events),
            json=json.loads(self._body) if is_json else None,
        )

    async def __aenter__(self) -> SseDrive:
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.leave()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), HANG_GUARD_S)
        finally:
            if not self._task.done():
                self._task.cancel()
                await asyncio.wait({self._task})
