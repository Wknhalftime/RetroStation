"""Starting a session engine until its harbor serves audio (spec: Errors "Liquidsoap fails to
start or never becomes ready | Retry once on another port"; D37 "5 s, then one retry on
another port"; Engine "Popen, not asyncio subprocesses", no blocking waits on the loop).
A Python script stands in for Liquidsoap, so these run on CI.

Ports: ``free_port()`` only says a port was free a moment ago. Under parallel workers another
process may bind it before the fake engine does, and then the retry on another port is the
correct behaviour. So each fake engine records the port it actually bound (``.served``), and
the tests check ``RunningEngine.port`` against that, not against the port first proposed.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from backend.playout.harbor import Upstream
from backend.playout.liquidsoap_process import (
    EngineConfig,
    EngineStartError,
    SessionEndpoint,
    free_port,
    start_ready_engine,
)

PYTHON = Path(getattr(sys, "_base_executable", sys.executable))
AUDIO = b"\xff\xfb" * 100

BIND = r"""
import os, pathlib, socket, sys, time
port = int(os.environ["HARBOR_PORT"])
server = socket.create_server(("127.0.0.1", port))
pathlib.Path(sys.argv[0] + ".served").write_text(str(port))
"""
ANSWER_503 = r"""
conn, _ = server.accept()
conn.recv(65536)
conn.sendall(b"HTTP/1.0 503 Service Unavailable\r\n\r\n")
conn.close()
"""
ANSWER_200 = r"""
conn, _ = server.accept()
conn.recv(65536)
conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: audio/mpeg\r\n\r\n" + b"\xff\xfb" * 100)
"""
HARBOR = BIND + ANSWER_200 + "conn.close()\n"
NOT_READY_THEN_HARBOR = BIND + ANSWER_503 + ANSWER_200 + "conn.close()\n"
HARBOR_THEN_HOLD = BIND + ANSWER_200 + "time.sleep(60)\n"
FIRST_LAUNCH_FAILS = (
    "import pathlib, sys\n"
    "marker = pathlib.Path(sys.argv[0] + '.launched')\n"
    "if not marker.exists():\n"
    "    marker.write_text('once')\n"
    "    sys.exit(4)\n" + HARBOR
)
NEVER_READY = "import time\ntime.sleep(60)\n"


def fake_engine(
    tmp_path: Path, body: str, ready_timeout_s: float
) -> tuple[EngineConfig, SessionEndpoint]:
    script = tmp_path / "engine.py"
    script.write_text(body, encoding="utf-8")
    engine = EngineConfig(
        exe=PYTHON,
        script=script,
        cache_dir=tmp_path / "cache",
        filler=tmp_path / "filler.flac",
        intro_sfx=None,
        ready_timeout_s=ready_timeout_s,
    )
    endpoint = SessionEndpoint(
        session_id="s1",
        harbor_port=free_port(),
        backend_url="http://127.0.0.1:8000/internal/stream/sessions/s1",
        session_token="tok",
        log_path=tmp_path / "s1.log",
    )
    return engine, endpoint


def served_port(tmp_path: Path) -> int:
    """The port the fake engine's harbor actually bound."""
    return int((tmp_path / "engine.py.served").read_text(encoding="utf-8"))


async def read_all(upstream: Upstream) -> bytes:
    data = b""
    while chunk := await upstream.read():
        data += chunk
    return data


def assert_gone(pid: int) -> None:
    """The process exits within 5 s (psutil.TimeoutExpired fails the test otherwise)."""
    with contextlib.suppress(psutil.NoSuchProcess):
        psutil.Process(pid).wait(timeout=5)


def slow_exit(monkeypatch: pytest.MonkeyPatch, delay_s: float) -> None:
    """Every Popen.wait first blocks ``delay_s``: a process slow to exit after its kill."""
    real_wait = subprocess.Popen.wait

    def wait(self: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        time.sleep(delay_s)
        return real_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait)


async def test_the_audio_is_handed_over_only_once_the_harbor_answers_200(tmp_path: Path) -> None:
    engine, endpoint = fake_engine(tmp_path, NOT_READY_THEN_HARBOR, ready_timeout_s=10.0)
    assigned: list[int] = []
    running = await start_ready_engine(assigned.append, os.environ, endpoint, engine)
    try:
        assert await read_all(running.upstream) == AUDIO
        assert running.pid == assigned[-1]
        assert running.port == served_port(tmp_path)
    finally:
        running.upstream.close()
        running.stop()


async def test_a_failed_first_start_is_retried_once_on_another_port(tmp_path: Path) -> None:
    engine, endpoint = fake_engine(tmp_path, FIRST_LAUNCH_FAILS, ready_timeout_s=10.0)
    assigned: list[int] = []
    running = await start_ready_engine(assigned.append, os.environ, endpoint, engine)
    try:
        assert len(assigned) == 2
        assert running.port != endpoint.harbor_port
        assert running.port == served_port(tmp_path)
        assert await read_all(running.upstream) == AUDIO
    finally:
        running.upstream.close()
        running.stop()


async def test_an_engine_that_exits_is_a_start_error_with_its_exit_code(tmp_path: Path) -> None:
    engine, endpoint = fake_engine(tmp_path, "import sys\nsys.exit(3)\n", ready_timeout_s=30.0)
    assigned: list[int] = []
    began = time.monotonic()
    with pytest.raises(EngineStartError, match="exit code 3"):
        await start_ready_engine(assigned.append, os.environ, endpoint, engine)
    assert len(assigned) == 2  # the one retry was made
    assert time.monotonic() - began < 10.0


async def test_an_engine_that_never_gets_ready_is_killed(tmp_path: Path) -> None:
    engine, endpoint = fake_engine(tmp_path, NEVER_READY, ready_timeout_s=0.5)
    pids: list[int] = []
    with pytest.raises(EngineStartError, match="not ready"):
        await start_ready_engine(pids.append, os.environ, endpoint, engine)
    assert len(pids) == 2
    for pid in pids:
        assert_gone(pid)


async def test_a_failed_start_is_reaped_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, endpoint = fake_engine(tmp_path, NEVER_READY, ready_timeout_s=0.5)
    slow_exit(monkeypatch, 1.0)
    gaps: list[float] = []

    async def ticker() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    pids: list[int] = []
    tick = asyncio.create_task(ticker())
    try:
        with pytest.raises(EngineStartError, match="not ready"):
            await start_ready_engine(pids.append, os.environ, endpoint, engine)
    finally:
        tick.cancel()
    assert max(gaps) < 0.5, f"the event loop stalled for {max(gaps):.2f} s"
    for pid in pids:
        assert_gone(pid)


async def test_a_refused_job_assignment_is_a_start_error(tmp_path: Path) -> None:
    engine, endpoint = fake_engine(tmp_path, NEVER_READY, ready_timeout_s=5.0)

    def refuse(pid: int) -> None:
        raise OSError("no job")

    with pytest.raises(EngineStartError, match="no job"):
        await start_ready_engine(refuse, os.environ, endpoint, engine)


async def test_stop_returns_at_once_and_the_engine_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, endpoint = fake_engine(tmp_path, HARBOR_THEN_HOLD, ready_timeout_s=10.0)
    running = await start_ready_engine(lambda pid: None, os.environ, endpoint, engine)
    running.upstream.close()
    slow_exit(monkeypatch, 2.0)  # a stop that waited for the exit would take >= 2 s
    began = time.monotonic()
    running.stop()
    running.stop()  # harmless twice
    assert time.monotonic() - began < 0.5
    assert_gone(running.pid)


def test_the_readiness_timeout_is_five_seconds_by_default(tmp_path: Path) -> None:
    engine = EngineConfig(
        exe=PYTHON,
        script=tmp_path / "engine.py",
        cache_dir=tmp_path / "cache",
        filler=tmp_path / "filler.flac",
        intro_sfx=None,
    )
    assert engine.ready_timeout_s == 5.0  # D37


def test_the_readiness_timeout_must_be_positive(tmp_path: Path) -> None:
    engine, _ = fake_engine(tmp_path, "", ready_timeout_s=1.0)
    with pytest.raises(ValueError, match="EngineConfig.ready_timeout_s"):
        dataclasses.replace(engine, ready_timeout_s=0.0)
