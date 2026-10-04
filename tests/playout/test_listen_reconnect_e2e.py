"""A same-key reconnect resumes from the still-open session, end to end (spec D109; plan
``2026-10-03-tune-in-reconnect-takeover.md`` Revision 2, R11; D6 "gone within ~1 s of
disconnect").

/listen -> StreamService -> D1's start_ready_engine -> real Liquidsoap -> internal API ->
relay, on a real uvicorn, as in ``test_listen_e2e.py``. The wall clock is fixed at 06:00:08, so
a clock tune-in always lands 8 s into Tone 0 (20 s long). The steady clock is the real one
(D47). The first listener's engine plays Tone 0 out and moves on to Tone 1. A second request
with the same key, while the first connection is still open, must then get Tone 1 (D109); a
clock tune-in would give Tone 0. Each tone has its own title, so the title a session shows
(ICY's now-playing) names its item.

Real time is waited on only for outcomes, each with a deadline (``wait_until``); no step is
ordered by a sleep. Every connection is read continuously by its own thread, as a player does.
"""

from __future__ import annotations

import contextlib
import http.client
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from ipaddress import IPv4Address
from pathlib import Path
from uuid import uuid4

import psutil
import pytest
import uvicorn
from fastapi import FastAPI
from stream_stub import ToneSpec, make_tone

from backend.domain.broadcast import BroadcastStation
from backend.domain.streaming import PlayableFile, ScheduleItem
from backend.playout.assets import ensure_stream_assets
from backend.playout.liquidsoap_process import (
    SESSION_SCRIPT,
    EngineConfig,
    RunningEngine,
    SessionEndpoint,
    free_port,
    session_base_env,
    start_ready_engine,
)
from backend.routers import listen, stream_internal
from backend.services.streaming.bookmarks import BookmarkStore
from backend.services.streaming.service import (
    StreamPorts,
    StreamRepos,
    StreamService,
    StreamServiceConfig,
)
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository
from tests.fakes.playable_schedule import FakePlayableScheduleRepository
from tests.fakes.user_settings import FakeUserSettingRepository
from tests.playout.test_session_liq_errors import job  # noqa: F401 - fixture
from tests.services.streaming.schedule import CALL, DAY, STATION, at

pytestmark = [pytest.mark.slow, pytest.mark.timeout(180)]

WALL = datetime(2026, 3, 14, 6, 0, 8)
"""The fixed wall clock: on DAY's station clock, 8 s into Tone 0."""
GONE_WITHIN_S = 1.0  # D6
FLOWING_BYTES = 16_384
"""Audio still arriving: this much more within the deadline."""
TONES = [("06:00:00", 20), ("06:00:20", 90), ("06:01:50", 90)]
"""(logged at, seconds) per tone: Tone 0 ends 12 s after a tune-in at WALL."""


def wait_until(condition: Callable[[], bool], timeout_s: float) -> bool:
    """Waits for an outcome up to a deadline (not ordering steps)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


@dataclass
class Listener:
    """One /listen connection, read continuously on its own thread until it is closed."""

    port: int
    key: str
    status: int = 0
    received: int = 0
    opened: threading.Event = field(default_factory=threading.Event)
    leave: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        assert self.opened.wait(30.0), f"harness: no response for key={self.key}"

    def _read(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request("GET", f"/listen/{CALL}/1995?key={self.key}")
            response = connection.getresponse()
            self.status = response.status
            self.opened.set()
            while not self.leave.is_set():
                chunk = response.read1(16_384)
                if not chunk:
                    return
                self.received += len(chunk)
        finally:
            self.opened.set()
            connection.close()

    def flowing(self, timeout_s: float = 15.0) -> bool:
        """Whether more audio keeps arriving."""
        before = self.received
        return wait_until(lambda: self.received >= before + FLOWING_BYTES, timeout_s)

    def close(self) -> None:
        self.leave.set()
        if self.thread is not None:
            self.thread.join(timeout=10)


@dataclass
class Harness:
    service: StreamService
    port: int
    started: list[tuple[str, int]]
    """(session id, engine pid) per engine start, in start order."""


@pytest.fixture
def harness(
    tmp_path: Path,
    liquidsoap_exe: Path,
    liq_cache: Path,
    job: object,  # noqa: F811
) -> Iterator[Harness]:
    items = [
        ScheduleItem(
            event_id=uuid4(),
            logged_at=at(hms),
            title=f"Tone {i}",
            artist="E2E",
            file=PlayableFile(
                file_id=uuid4(),
                path=str(make_tone("ffmpeg", tmp_path / f"t{i}.flac", ToneSpec(400 + 100 * i, s))),
                duration_ms=s * 1000,
                cues=None,
            ),
        )
        for i, (hms, s) in enumerate(TONES)
    ]
    schedule = FakePlayableScheduleRepository()
    schedule.set_day(STATION, DAY, items)
    stations = FakeBroadcastStationRepository()
    stations.create(BroadcastStation(id=STATION, call_letters=CALL))
    repos = StreamRepos(stations=stations, settings=FakeUserSettingRepository(), schedule=schedule)

    def open_repos() -> AbstractContextManager[StreamRepos]:
        return nullcontext(repos)

    assets = ensure_stream_assets("ffmpeg", tmp_path / "assets")
    engine = EngineConfig(
        exe=liquidsoap_exe,
        script=SESSION_SCRIPT,
        cache_dir=liq_cache,
        filler=assets.filler,
        intro_sfx=assets.static_intro,
    )
    started: list[tuple[str, int]] = []

    async def start(endpoint: SessionEndpoint) -> RunningEngine:
        running = await start_ready_engine(
            job.assign,  # type: ignore[attr-defined]
            session_base_env(os.environ),
            endpoint,
            engine=engine,
        )
        started.append((endpoint.session_id, running.pid))
        return running

    port = free_port()
    (tmp_path / "logs").mkdir()
    service = StreamService(
        StreamPorts(
            repos=open_repos, start_engine=start, clock=lambda: WALL, steady=time.monotonic
        ),
        BookmarkStore(),
        StreamServiceConfig(
            callback_base_url=f"http://127.0.0.1:{port}", log_dir=tmp_path / "logs"
        ),
    )
    app = FastAPI()
    app.include_router(listen.router)
    app.include_router(stream_internal.router)
    app.state.stream_service = service
    app.state.server_host = IPv4Address("127.0.0.1")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert wait_until(lambda: server.started, 10.0), "harness: uvicorn did not start"
        yield Harness(service, port, started)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        service.close_all()


def alive(pid: int) -> bool:
    with contextlib.suppress(psutil.NoSuchProcess):
        return psutil.Process(pid).is_running()
    return False


def test_a_same_key_request_gets_the_open_sessions_current_item(harness: Harness) -> None:
    # R11 (D109): a keyed listener plays Tone 0 out and moves on to Tone 1; a second request
    # with the same key, while the first connection is open, plays Tone 1 on a running stream
    # (a clock tune-in would play Tone 0); the first keeps running until its connection closes.
    service = harness.service
    first = Listener(harness.port, "e2e")
    second = Listener(harness.port, "e2e")
    try:
        first.start()
        assert first.status == 200
        [(first_id, first_pid)] = harness.started
        assert wait_until(lambda: service.now_playing(first_id) == "E2E - Tone 1", 45.0), (
            f"harness: the first stream shows {service.now_playing(first_id)!r}, not Tone 1"
        )
        second.start()
        assert second.status == 200
        [_, (second_id, second_pid)] = harness.started
        assert wait_until(lambda: service.now_playing(second_id) != "", 20.0), "no title"
        assert service.now_playing(second_id) == "E2E - Tone 1"
        assert second.flowing(), "the second stream is not running"
        # The first is untouched: still open, its engine running, its audio still arriving.
        assert first.flowing(10.0), "the first stream stopped"
        assert alive(first_pid)
        assert service.open_sessions == 2
        first.close()  # ... until its own connection closes
        with contextlib.suppress(psutil.NoSuchProcess):
            psutil.Process(first_pid).wait(timeout=GONE_WITHIN_S)
        assert wait_until(lambda: service.open_sessions == 1, 5.0)
        assert second.flowing(10.0), "the second stream stopped when the first closed"
        assert alive(second_pid)
    finally:
        first.close()
        second.close()
