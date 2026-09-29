"""Reach a session engine's harbor (spec: Engine, relay "retrying upstream until it gets a 200").

The connection that finds the harbor ready is the listener's upstream, so readiness costs
no extra connection and no extra wait.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from backend.playout.errors import EngineStartError

_READ_CHUNK = 16 * 1024
_RETRY_S = 0.05  # loopback answers in about 1 ms; the deadline bounds the attempts
_HEAD_END = b"\r\n\r\n"
_MAX_HEAD = 64 * 1024


@dataclass(frozen=True)
class Upstream:
    """The harbor's MP3 body: ``read`` gives up to 16 KiB, ``b""`` at the end."""

    read: Callable[[], Awaitable[bytes]]
    close: Callable[[], None]


def _status_of(head: bytes) -> int | None:
    parts = head.split(b" ", 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/") or not parts[1].isdigit():
        return None
    return int(parts[1])


async def _answer(port: int) -> Upstream | None:
    """One ``GET /stream``: the upstream if the harbor answers 200, else ``None``.

    A refused connect, a dropped connection or any other status is ``None``. The connection
    is closed on every path but a 200, including when the caller's deadline cancels us.
    """
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except ConnectionRefusedError:
        return None
    upstream: Upstream | None = None
    try:
        writer.write(b"GET /stream HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        head = await reader.readuntil(_HEAD_END)
        if _status_of(head[:_MAX_HEAD]) == 200:
            upstream = Upstream(read=lambda: reader.read(_READ_CHUNK), close=writer.close)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        return None
    finally:
        if upstream is None:
            writer.close()
    return upstream


async def open_upstream(port: int, timeout_s: float, alive: Callable[[], bool]) -> Upstream:
    """Connect to the harbor on ``port`` until it answers ``GET /stream`` with 200.

    Raises ``EngineStartError`` at once when ``alive()`` turns false, and after
    ``timeout_s`` when the harbor has not answered 200.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    not_ready = EngineStartError(f"harbor on {port} not ready after {timeout_s} s")
    while True:
        if not alive():
            raise EngineStartError(f"engine exited before its harbor on {port} was ready")
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise not_ready
        try:
            async with asyncio.timeout(remaining):
                upstream = await _answer(port)
        except TimeoutError:
            raise not_ready from None
        if upstream is not None:
            return upstream
        await asyncio.sleep(_RETRY_S)
