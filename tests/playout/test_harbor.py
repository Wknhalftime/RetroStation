"""Reaching a session's harbor (spec: Engine, relay "retrying upstream until it gets a 200";
Errors "Liquidsoap fails to start or never becomes ready"). A fake harbor stands in."""

from __future__ import annotations

import asyncio
import socket
import time

import pytest

from backend.playout.harbor import Upstream, open_upstream
from backend.playout.liquidsoap_process import EngineStartError, free_port


async def harbor(statuses: list[int], body: bytes) -> tuple[asyncio.Server, list[bytes]]:
    """A fake harbor: answers each connection with the next status; then 200 with ``body``."""
    requests: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        status = statuses.pop(0) if statuses else 200
        writer.write(f"HTTP/1.0 {status} X\r\nContent-Type: audio/mpeg\r\n\r\n".encode())
        if status == 200:
            writer.write(body)
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0), requests


def port_of(server: asyncio.Server) -> int:
    return int(server.sockets[0].getsockname()[1])


async def read_all(upstream: Upstream) -> bytes:
    data = b""
    while chunk := await upstream.read():
        data += chunk
    return data


async def test_the_upstream_is_retried_until_the_harbor_answers_200() -> None:
    server, requests = await harbor([503, 404], b"MP3-BYTES")
    async with server:
        upstream = await open_upstream(port_of(server), timeout_s=5.0, alive=lambda: True)
        try:
            assert await read_all(upstream) == b"MP3-BYTES"
        finally:
            upstream.close()
    assert len(requests) == 3
    assert all(request.startswith(b"GET /stream HTTP/1.") for request in requests)


async def test_a_harbor_that_never_answers_200_is_not_ready() -> None:
    server, _ = await harbor([503] * 10_000, b"")
    async with server:
        began = time.monotonic()
        with pytest.raises(EngineStartError, match="not ready"):
            await open_upstream(port_of(server), timeout_s=0.3, alive=lambda: True)
    assert time.monotonic() - began < 3.0


async def test_a_dead_engine_fails_at_once() -> None:
    began = time.monotonic()
    with pytest.raises(EngineStartError, match="exited"):
        await open_upstream(free_port(), timeout_s=30.0, alive=lambda: False)
    assert time.monotonic() - began < 2.0


async def test_a_harbor_that_is_not_listening_yet_is_retried() -> None:
    # The port is bound but not listening, so connects are refused (on Windows only after
    # about 2 s); the harbor starts listening 3 s later. Holding the socket keeps the port
    # ours under parallel workers.
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    port = int(held.getsockname()[1])

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n\r\nMP3-BYTES")
        await writer.drain()
        writer.close()

    async def listen_later() -> asyncio.Server:
        await asyncio.sleep(3.0)
        return await asyncio.start_server(handle, sock=held)

    later = asyncio.create_task(listen_later())
    try:
        upstream = await open_upstream(port, timeout_s=10.0, alive=lambda: True)
        try:
            assert await read_all(upstream) == b"MP3-BYTES"
        finally:
            upstream.close()
    finally:
        server = await later
        server.close()
        await server.wait_closed()


async def test_a_harbor_that_accepts_but_never_answers_is_not_ready() -> None:
    closed = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()  # b"" once the client closes its end
        closed.set()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        began = time.monotonic()
        with pytest.raises(EngineStartError, match="not ready"):
            await asyncio.wait_for(
                open_upstream(port_of(server), timeout_s=0.3, alive=lambda: True), 10
            )
        assert time.monotonic() - began < 3.0
        await asyncio.wait_for(closed.wait(), 2.0)  # the abandoned attempt was closed
    finally:
        server.close()
        server.close_clients()  # a leaked connection must not hang wait_closed()
        await server.wait_closed()
