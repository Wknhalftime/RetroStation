"""Relay one session's MP3 to one listener (spec: Engine relay.py: "pipes MP3 from Liquidsoap
to the client ... drops a client after 15 s without write progress; inserts ICY StreamTitle
when the client sends Icy-MetaData: 1"; D13; D25).

Byte handling only: it takes primitives and imports nothing else from backend.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from backend.playout.harbor import Upstream

__all__ = [
    "AsgiApp",
    "Client",
    "Framing",
    "IcyFramer",
    "Message",
    "Receive",
    "RelayConfig",
    "RelayEnd",
    "Scope",
    "Send",
    "client_framing",
    "icy_block",
    "relay",
    "relay_asgi",
]

# Incoming ASGI data is read-only here, so any server's mapping fits; what we send is a dict.
type Scope = Mapping[str, object]
type Message = dict[str, object]
type Receive = Callable[[], Awaitable[Mapping[str, object]]]
type Send = Callable[[Message], Awaitable[None]]
type AsgiApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_ICY_MAX_BLOCKS = 255
_ICY_BLOCK = 16
_TITLE_OPEN = b"StreamTitle='"
_TITLE_CLOSE = b"';"
_TITLE_ROOM = _ICY_MAX_BLOCKS * _ICY_BLOCK - len(_TITLE_OPEN) - len(_TITLE_CLOSE)


class RelayEnd(StrEnum):
    """How a relay ended."""

    UPSTREAM_ENDED = "upstream_ended"
    CLIENT_GONE = "client_gone"
    CLIENT_STALLED = "client_stalled"


@dataclass(frozen=True)
class RelayConfig:
    """``stall_s``: the longest one write may take; ``icy_metaint``: audio bytes per title."""

    stall_s: float = 15.0  # Errors: "Client stall > 15 s: Dropped"
    icy_metaint: int = 16_000

    def __post_init__(self) -> None:
        for name, value in (("stall_s", self.stall_s), ("icy_metaint", self.icy_metaint)):
            if value <= 0:
                raise ValueError(f"RelayConfig.{name} must be > 0, got {value}")


@dataclass(frozen=True)
class Client:
    """Where bytes go: ``write`` them, framed by ``frame``; ``gone`` is set when it leaves."""

    write: Callable[[bytes], Awaitable[None]]
    gone: asyncio.Event
    frame: Callable[[bytes], bytes]


async def _finished_first[T](
    work: asyncio.Task[T], gone: asyncio.Task[bool], timeout_s: float | None
) -> bool:
    """Wait for ``work`` or ``gone`` (at most ``timeout_s``); True if only ``work`` is done."""
    done, _ = await asyncio.wait(
        {work, gone}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
    )
    return work in done and gone not in done


async def _cancel_and_settle(*tasks: asyncio.Task[object] | None) -> None:
    """Cancel each unfinished task, then wait until every one of them has ended."""
    unfinished = {task for task in tasks if task is not None and not task.done()}
    for task in unfinished:
        task.cancel()
    if unfinished:
        await asyncio.wait(unfinished)


async def relay(upstream: Upstream, client: Client, config: RelayConfig) -> RelayEnd:
    """Copy the upstream to the client until either ends or one write outlasts ``stall_s``.

    The stall limit applies to each write, so a slow client that keeps reading is kept.
    On every way out, cancellation from outside included, the upstream is closed and the
    in-flight read or write is cancelled and awaited.
    """
    gone = asyncio.ensure_future(client.gone.wait())
    in_flight: asyncio.Task[object] | None = None
    try:
        while True:
            read = asyncio.ensure_future(upstream.read())
            in_flight = read
            if not await _finished_first(read, gone, None):
                return RelayEnd.CLIENT_GONE
            lost = read.exception()
            if isinstance(lost, OSError):  # the harbor's connection dropped: nothing more
                return RelayEnd.UPSTREAM_ENDED
            if lost is not None:
                raise lost
            chunk = read.result()
            if not chunk:
                return RelayEnd.UPSTREAM_ENDED
            write = asyncio.ensure_future(client.write(client.frame(chunk)))
            in_flight = write
            if not await _finished_first(write, gone, config.stall_s):
                return RelayEnd.CLIENT_GONE if gone.done() else RelayEnd.CLIENT_STALLED
            failure = write.exception()
            if isinstance(failure, OSError):  # reset, aborted or broken: the listener left
                return RelayEnd.CLIENT_GONE
            if failure is not None:
                raise failure
    finally:
        upstream.close()  # first, so a second cancellation during the settle cannot skip it
        await _cancel_and_settle(gone, in_flight)


def icy_block(title: str) -> bytes:
    """One ICY metadata block: a length byte, then ``StreamTitle='...';`` NUL-padded to 16.

    The title is UTF-8 (an unencodable character, such as a lone surrogate, becomes ``?``),
    cut at a character boundary so the block fits 255 x 16 bytes.
    """
    fitted = title.encode(errors="replace")[:_TITLE_ROOM].decode(errors="ignore").encode()
    payload = _TITLE_OPEN + fitted + _TITLE_CLOSE
    blocks = math.ceil(len(payload) / _ICY_BLOCK)
    return bytes([blocks]) + payload.ljust(blocks * _ICY_BLOCK, b"\x00")


class IcyFramer:
    """Inserts a metadata block every ``metaint`` audio bytes: the title when it changed
    (always the first time), else an empty block."""

    def __init__(self, metaint: int, title: Callable[[], str]) -> None:
        if metaint <= 0:
            raise ValueError(f"IcyFramer.metaint must be > 0, got {metaint}")
        self._metaint = metaint
        self._title = title
        self._until_block = metaint
        self._sent: str | None = None

    def frame(self, chunk: bytes) -> bytes:
        out = bytearray()
        rest = memoryview(chunk)
        while len(rest) >= self._until_block:
            out += rest[: self._until_block]
            rest = rest[self._until_block :]
            out += self._block()
            self._until_block = self._metaint
        out += rest
        self._until_block -= len(rest)
        return bytes(out)

    def _block(self) -> bytes:
        title = self._title()
        if title == self._sent:
            return b"\x00"
        self._sent = title
        return icy_block(title)


@dataclass(frozen=True)
class Framing:
    """The response headers and the byte framing one client asked for."""

    headers: tuple[tuple[bytes, bytes], ...]
    frame: Callable[[bytes], bytes]


def _unchanged(chunk: bytes) -> bytes:
    return chunk


def client_framing(
    icy_metadata: str | None, title: Callable[[], str], config: RelayConfig
) -> Framing:
    """Plain MP3, or MP3 with ICY titles when the client sent ``Icy-MetaData: 1``."""
    headers = ((b"content-type", b"audio/mpeg"), (b"cache-control", b"no-cache, no-store"))
    if icy_metadata != "1":
        return Framing(headers, _unchanged)
    metaint = (b"icy-metaint", str(config.icy_metaint).encode())
    return Framing((*headers, metaint), IcyFramer(config.icy_metaint, title).frame)


async def _watch_for_disconnect(receive: Receive, gone: asyncio.Event) -> None:
    """Set ``gone`` on ``http.disconnect``, and on any other exit: a watcher that can no
    longer see the listener must not keep the stream (and its engine) running."""
    try:
        while (await receive())["type"] != "http.disconnect":
            pass
    finally:
        gone.set()


async def _relay_response(
    upstream: Upstream, framing: Framing, config: RelayConfig, receive: Receive, send: Send
) -> None:
    """Relay the body while a watcher marks the client gone on ``http.disconnect``.

    A watcher that failed is re-raised once the relay has ended.
    """
    gone = asyncio.Event()

    async def write(chunk: bytes) -> None:
        await send({"type": "http.response.body", "body": chunk, "more_body": True})

    watcher = asyncio.ensure_future(_watch_for_disconnect(receive, gone))
    try:
        end = await relay(upstream, Client(write, gone, framing.frame), config)
        if end is RelayEnd.UPSTREAM_ENDED:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
    finally:
        await _cancel_and_settle(watcher)
    failure = None if watcher.cancelled() else watcher.exception()
    if failure is not None:
        raise failure


def relay_asgi(
    upstream: Upstream, framing: Framing, on_end: Callable[[], None], config: RelayConfig
) -> AsgiApp:
    """An ASGI app that relays ``upstream`` to the requesting client; ``on_end`` runs once
    when the response ends, however it ends.

    Only ``UPSTREAM_ENDED`` completes the response (a final empty body). On
    ``CLIENT_STALLED`` or ``CLIENT_GONE`` the app returns with the response unfinished:
    the server then closes the transport, and that is what drops a stalled client.
    """

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        relayed = False  # the relay closes the upstream; before it starts, we must
        try:
            await send(
                {"type": "http.response.start", "status": 200, "headers": list(framing.headers)}
            )
            relayed = True
            await _relay_response(upstream, framing, config, receive, send)
        finally:
            if not relayed:
                upstream.close()
            on_end()

    return app
