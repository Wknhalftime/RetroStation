"""The relay between one harbor and one listener (spec: Engine relay.py: "pipes MP3 from
Liquidsoap to the client ... drops a client after 15 s without write progress; inserts ICY
StreamTitle when the client sends Icy-MetaData: 1"; D25; Errors "Client stall > 15 s:
Dropped"; Testing "Relay: a fake upstream that emits bytes")."""

from __future__ import annotations

import asyncio
import math
import time

import pytest

from backend.playout.harbor import Upstream
from backend.playout.relay import (
    Client,
    IcyFramer,
    RelayConfig,
    RelayEnd,
    client_framing,
    icy_block,
    relay,
    relay_asgi,
)


def unchanged(chunk: bytes) -> bytes:
    return chunk


def finite(*chunks: bytes) -> tuple[Upstream, list[str]]:
    pending = list(chunks)
    closed: list[str] = []

    async def read() -> bytes:
        return pending.pop(0) if pending else b""

    return Upstream(read=read, close=lambda: closed.append("closed")), closed


def endless(chunk: bytes = b"\xff" * 1024) -> tuple[Upstream, list[str]]:
    closed: list[str] = []

    async def read() -> bytes:
        await asyncio.sleep(0)
        return chunk

    return Upstream(read=read, close=lambda: closed.append("closed")), closed


# ---- relaying ------------------------------------------------------------------------------


async def test_bytes_pass_through_in_order_until_the_upstream_ends() -> None:
    upstream, closed = finite(b"ab", b"cd", b"ef")
    written: list[bytes] = []

    async def write(chunk: bytes) -> None:
        written.append(chunk)

    end = await relay(upstream, Client(write, asyncio.Event(), unchanged), RelayConfig())
    assert end is RelayEnd.UPSTREAM_ENDED
    assert b"".join(written) == b"abcdef"
    assert closed == ["closed"]


async def test_a_departed_client_stops_the_relay() -> None:
    upstream, closed = endless()
    gone = asyncio.Event()
    writes = 0

    async def write(chunk: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 3:
            gone.set()

    relaying = relay(upstream, Client(write, gone, unchanged), RelayConfig())
    end = await asyncio.wait_for(relaying, timeout=2.0)
    assert end is RelayEnd.CLIENT_GONE
    assert closed == ["closed"]


async def test_a_reset_connection_counts_as_a_departed_client() -> None:
    upstream, closed = endless()

    async def write(chunk: bytes) -> None:
        raise ConnectionResetError("peer reset")

    relaying = relay(upstream, Client(write, asyncio.Event(), unchanged), RelayConfig())
    end = await asyncio.wait_for(relaying, timeout=2.0)
    assert end is RelayEnd.CLIENT_GONE
    assert closed == ["closed"]


async def test_a_stalled_client_is_dropped_after_the_stall_limit() -> None:
    upstream, closed = endless()

    async def write(chunk: bytes) -> None:
        await asyncio.Event().wait()  # the player stopped reading; the socket never drains

    began = time.monotonic()
    relaying = relay(upstream, Client(write, asyncio.Event(), unchanged), RelayConfig(stall_s=0.2))
    end = await asyncio.wait_for(relaying, timeout=5.0)
    elapsed = time.monotonic() - began
    assert end is RelayEnd.CLIENT_STALLED
    assert 0.2 <= elapsed < 2.0
    assert closed == ["closed"]


async def test_a_slow_client_that_keeps_reading_is_kept() -> None:
    upstream, _ = finite(*[b"x"] * 5)

    async def write(chunk: bytes) -> None:
        await asyncio.sleep(0.05)

    end = await relay(upstream, Client(write, asyncio.Event(), unchanged), RelayConfig(stall_s=0.5))
    assert end is RelayEnd.UPSTREAM_ENDED


async def test_a_client_that_keeps_reading_is_kept_past_the_stall_limit() -> None:
    # "15 s without write progress": the limit applies to each write, not to the whole
    # relay. 20 writes of 0.05 s each (1 s in all) outlast a 0.5 s limit.
    upstream, _ = finite(*[b"x"] * 20)

    async def write(chunk: bytes) -> None:
        await asyncio.sleep(0.05)

    relaying = relay(upstream, Client(write, asyncio.Event(), unchanged), RelayConfig(stall_s=0.5))
    end = await asyncio.wait_for(relaying, timeout=5.0)
    assert end is RelayEnd.UPSTREAM_ENDED


def test_the_default_stall_limit_is_15_seconds() -> None:
    assert RelayConfig().stall_s == 15.0


@pytest.mark.parametrize("field", ["stall_s", "icy_metaint"])
def test_relay_config_rejects_non_positive_values(field: str) -> None:
    with pytest.raises(ValueError, match=f"RelayConfig.{field}"):
        RelayConfig(**{field: 0})  # type: ignore[arg-type]


# ---- ICY (D13, D25) ------------------------------------------------------------------------


def test_an_icy_block_carries_the_title_padded_to_16_bytes() -> None:
    block = icy_block("ABBA - Fernando")
    payload = b"StreamTitle='ABBA - Fernando';"
    assert block[0] == math.ceil(len(payload) / 16)
    assert len(block) == 1 + 16 * block[0]
    assert block[1 : 1 + len(payload)] == payload
    assert set(block[1 + len(payload) :]) <= {0}


def test_an_icy_block_is_utf8() -> None:
    assert "Beyoncé - Halo".encode() in icy_block("Beyoncé - Halo")


def test_an_icy_block_is_capped_at_255_blocks_and_keeps_its_terminator() -> None:
    block = icy_block("x" * 5000)
    assert block[0] == 255
    assert len(block) == 1 + 255 * 16
    assert block[1:].rstrip(b"\x00").endswith(b"';")


def test_the_framer_inserts_metadata_every_metaint_bytes_across_chunks() -> None:
    framer = IcyFramer(4, lambda: "A - T")
    out = b"".join(framer.frame(chunk) for chunk in [b"abc", b"defgh", b"ij"])
    assert out == b"abcd" + icy_block("A - T") + b"efgh" + b"\x00" + b"ij"


def test_the_framer_sends_a_title_only_when_it_changes() -> None:
    titles = iter(["A - One", "A - One", "B - Two"])
    framer = IcyFramer(2, lambda: next(titles))
    out = framer.frame(b"aabbcc")
    assert out == b"aa" + icy_block("A - One") + b"bb" + b"\x00" + b"cc" + icy_block("B - Two")


def test_a_plain_client_gets_mp3_headers_and_untouched_bytes() -> None:
    framing = client_framing(None, lambda: "A - T", RelayConfig())
    headers = dict(framing.headers)
    assert headers[b"content-type"] == b"audio/mpeg"
    assert b"icy-metaint" not in headers
    assert framing.frame(b"x" * 20_000) == b"x" * 20_000


def test_an_icy_client_gets_metaint_and_titled_frames() -> None:
    framing = client_framing("1", lambda: "A - T", RelayConfig(icy_metaint=4))
    assert dict(framing.headers)[b"icy-metaint"] == b"4"
    assert framing.frame(b"abcdef") == b"abcd" + icy_block("A - T") + b"ef"


# ---- the ASGI adapter (the relay behind /listen) --------------------------------------------


async def test_the_asgi_relay_ends_when_the_listener_disconnects() -> None:
    upstream, closed = endless()
    ended: list[str] = []
    framing = client_framing(None, lambda: "", RelayConfig())
    app = relay_asgi(upstream, framing, lambda: ended.append("end"), RelayConfig())
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await asyncio.wait_for(app({"type": "http"}, receive, send), timeout=2.0)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    assert closed == ["closed"]
    assert ended == ["end"]


async def test_the_asgi_relay_completes_the_response_when_the_upstream_ends() -> None:
    upstream, _ = finite(b"ab", b"cd")
    ended: list[str] = []
    framing = client_framing(None, lambda: "", RelayConfig())
    app = relay_asgi(upstream, framing, lambda: ended.append("end"), RelayConfig())
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        await asyncio.Event().wait()  # the listener stays
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await asyncio.wait_for(app({"type": "http"}, receive, send), timeout=2.0)
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert b"".join(m["body"] for m in bodies) == b"abcd"  # type: ignore[misc]
    assert bodies[-1].get("more_body", False) is False
    assert ended == ["end"]
