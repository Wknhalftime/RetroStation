"""A listener who gives up mid-tune-in leaves nothing running (spec D6: "One Liquidsoap
process per listener, started on connect, gone within ~1 s of disconnect. Nothing runs with
no listeners."; new D2 requirement, 2026-09-29: the client disconnects while the engine is
still starting).

/listen -> StreamService -> D1's start_ready_engine, unmodified. The engine is a stand-in
process that never becomes ready, so the disconnect always lands mid-start: the harbor never
answers, and D1 would otherwise wait out its 5 s readiness timeout and retry. The ASGI app is
called directly, so the test decides exactly when the client leaves."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from contextlib import AbstractContextManager, nullcontext
from functools import partial
from pathlib import Path

import psutil
import pytest
from fastapi import FastAPI

from backend.domain.broadcast import BroadcastStation
from backend.playout.liquidsoap_process import (
    EngineConfig,
    session_base_env,
    start_ready_engine,
)
from backend.routers import listen
from backend.services.streaming.bookmarks import BookmarkStore
from backend.services.streaming.service import (
    StreamPorts,
    StreamRepos,
    StreamService,
    StreamServiceConfig,
)
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository
from tests.fakes.user_settings import FakeUserSettingRepository
from tests.services.streaming.helpers import CountingSchedule
from tests.services.streaming.schedule import CALL, DAY, NOW, STATION, song

# The real interpreter, not a venv launcher: killing a launcher would orphan its child.
STAND_IN = Path(getattr(sys, "_base_executable", sys.executable))
if re.search(r"\\\d", str(STAND_IN)):
    pytest.skip("the interpreter path contains a backslash-digit", allow_module_level=True)

GONE_WITHIN_S = 1.0  # D6: "gone within ~1 s of disconnect"


def listen_scope(path: str) -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"key=car",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("192.168.1.30", 50123),
        "server": ("127.0.0.1", 8010),
    }


def gone_within(pid: int, timeout_s: float) -> bool:
    """Whether process ``pid`` has exited within ``timeout_s`` (blocking; run in a thread)."""
    try:
        psutil.Process(pid).wait(timeout=timeout_s)
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        return False
    return True


async def test_a_listener_who_gives_up_mid_tune_in_leaves_nothing_running(
    tmp_path: Path,
) -> None:
    never_ready = tmp_path / "never_ready.py"
    never_ready.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    engine = EngineConfig(
        exe=STAND_IN,
        script=never_ready,
        cache_dir=tmp_path / "cache",
        filler=tmp_path / "filler.flac",
        intro_sfx=None,
    )
    loop = asyncio.get_running_loop()
    pids: list[int] = []
    spawned = asyncio.Event()

    def assign(pid: int) -> None:  # called from D1's worker thread
        pids.append(pid)
        loop.call_soon_threadsafe(spawned.set)

    stations = FakeBroadcastStationRepository()
    stations.create(BroadcastStation(id=STATION, call_letters=CALL))
    schedule = CountingSchedule()
    schedule.set_day(STATION, DAY, [song("06:00:00"), song("06:03:20")])
    repos = StreamRepos(stations=stations, settings=FakeUserSettingRepository(), schedule=schedule)

    def open_repos() -> AbstractContextManager[StreamRepos]:
        return nullcontext(repos)

    (tmp_path / "logs").mkdir()
    service = StreamService(
        StreamPorts(
            repos=open_repos,
            start_engine=partial(
                start_ready_engine, assign, session_base_env(os.environ), engine=engine
            ),
            clock=lambda: NOW,
        ),
        BookmarkStore(),
        StreamServiceConfig(callback_base_url="http://127.0.0.1:8010", log_dir=tmp_path / "logs"),
    )
    app = FastAPI()
    app.include_router(listen.router)
    app.state.stream_service = service

    gave_up = asyncio.Event()
    requested = False

    async def receive() -> dict[str, object]:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await gave_up.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        return None

    request = asyncio.create_task(app(listen_scope("/listen/KIOA/1995"), receive, send))
    try:
        await asyncio.wait_for(spawned.wait(), timeout=10.0)
        [pid] = pids
        assert psutil.pid_exists(pid), "harness: the stand-in engine is not running"
        assert service.open_sessions == 1, "harness: the listener was not admitted"
        assert not request.done(), "harness: the tune-in ended before the listener left"

        gave_up.set()
        left = time.monotonic()
        assert await asyncio.to_thread(gone_within, pid, GONE_WITHIN_S), (
            f"engine {pid} still running {GONE_WITHIN_S} s after the listener left"
        )
        await asyncio.wait_for(request, timeout=max(0.0, left + GONE_WITHIN_S - time.monotonic()))
        assert service.open_sessions == 0
    finally:
        gave_up.set()
        service.close_all()
        for pid in pids:
            if psutil.pid_exists(pid):
                psutil.Process(pid).kill()
        if not request.done():
            request.cancel()
