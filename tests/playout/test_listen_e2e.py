"""First stream end to end (spec Delivery row D: "first stream playable in VLC"; D6 "gone
within ~1 s of disconnect"; D30 the bookmark on leaving; D40/D44 first audio).
/listen -> StreamService -> D1's start_ready_engine -> real Liquidsoap -> internal API ->
relay, on a real uvicorn. Deterministic: a fixed clock and schedule; process exit awaited
with psutil.

D44: the locked bound is D2's own overhead, first_audio - max(engine_ready, day_read) <= 0.3 s,
where engine_ready and day_read are how long the engine start and the day read each took.
It holds under disk load. The absolute gates (warm <= 1.5 s, cold <= 2.0 s) are measured by
hand on a quiet machine at D2's finish."""

from __future__ import annotations

import contextlib
import http.client
import os
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import date
from ipaddress import IPv4Address
from pathlib import Path
from uuid import UUID, uuid4

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
from backend.services.streaming.bookmarks import BookmarkKey, BookmarkStore
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
from tests.services.streaming.schedule import CALL, DAY, NOW, STATION, YEAR, at

pytestmark = [pytest.mark.slow, pytest.mark.timeout(180)]


def wait_until(condition: Callable[[], bool], timeout_s: float) -> bool:
    """Waits for an outcome up to a deadline (not ordering steps)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


OVERHEAD_LIMIT_S = 0.3  # D44
GONE_WITHIN_S = 1.0  # D6: "gone within ~1 s of disconnect"


class ColdSchedule(FakePlayableScheduleRepository):
    """The first get_day takes ``cold_s`` (a cold PostgreSQL read took 1.6 s on dev: R1).
    ``read_s`` is the longest any day read took."""

    def __init__(self, cold_s: float) -> None:
        super().__init__()
        self.cold_s = cold_s
        self.read_s = 0.0

    def get_day(self, station_id: UUID, day: date) -> list[ScheduleItem]:
        began = time.monotonic()
        delay, self.cold_s = self.cold_s, 0.0
        time.sleep(delay)
        items = super().get_day(station_id, day)
        self.read_s = max(self.read_s, time.monotonic() - began)
        return items


@pytest.mark.parametrize("cold_s", [0.0, 1.6], ids=["warm", "cold"])
def test_a_listener_hears_the_station_and_leaving_frees_everything(
    tmp_path: Path,
    liquidsoap_exe: Path,
    liq_cache: Path,
    job: object,  # noqa: F811
    cold_s: float,
) -> None:
    tones = [
        make_tone("ffmpeg", tmp_path / f"t{i}.flac", ToneSpec(400 + 100 * i, 90.0))
        for i in range(2)
    ]
    items = [
        ScheduleItem(
            event_id=uuid4(),
            logged_at=at(hms),
            title=f"Tone {i}",
            artist="E2E",
            file=PlayableFile(file_id=uuid4(), path=str(tone), duration_ms=90_000, cues=None),
        )
        for i, (hms, tone) in enumerate(zip(["06:00:00", "06:01:30"], tones, strict=True))
    ]
    schedule = ColdSchedule(cold_s)
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
    pids: list[int] = []

    def assign(pid: int) -> None:
        pids.append(pid)
        job.assign(pid)  # type: ignore[attr-defined]

    engine_ready_s: list[float] = []

    async def timed_start(endpoint: SessionEndpoint) -> RunningEngine:
        began = time.monotonic()
        running = await start_ready_engine(
            assign, session_base_env(os.environ), endpoint, engine=engine
        )
        engine_ready_s.append(time.monotonic() - began)
        return running

    port = free_port()
    (tmp_path / "logs").mkdir()
    bookmarks = BookmarkStore()
    service = StreamService(
        StreamPorts(
            repos=open_repos,
            start_engine=timed_start,
            clock=lambda: NOW,
        ),
        bookmarks,
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
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        began = time.monotonic()
        connection.request("GET", "/listen/KIOA/1995?key=e2e")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("content-type") == "audio/mpeg"
        heard = response.read1(1)
        first_audio_s = time.monotonic() - began
        assert heard, "no audio"
        # D44, R1: the day read overlaps the engine start, and D2 adds little to either.
        [engine_s] = engine_ready_s
        overhead_s = first_audio_s - max(engine_s, schedule.read_s)
        assert overhead_s <= OVERHEAD_LIMIT_S, (
            f"first audio {first_audio_s:.3f} s, engine ready in {engine_s:.3f} s, "
            f"day read in {schedule.read_s:.3f} s: overhead {overhead_s:.3f} s"
        )
        deadline = time.monotonic() + 15.0
        while len(heard) < 65_536 and time.monotonic() < deadline:
            heard += response.read1(16_384)
        assert len(heard) >= 65_536, f"only {len(heard)} bytes in 15 s"
        response.close()
        connection.close()
        assert pids, "harness: no engine started"
        for pid in pids:  # D6: the engine is gone within ~1 s of the listener leaving
            with contextlib.suppress(psutil.NoSuchProcess):
                psutil.Process(pid).wait(timeout=GONE_WITHIN_S)
        assert wait_until(lambda: service.open_sessions == 0, 5.0)
        kept = bookmarks.get(BookmarkKey("e2e", STATION, YEAR))
        assert kept is not None and kept.event_id == items[0].event_id
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        service.close_all()
