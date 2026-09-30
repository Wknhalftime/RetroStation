"""The stream service rig: the existing repository fakes, a fake engine start and an
explicit clock (spec, Testing: "Service: in-memory fakes"; project rule: fakes implement the
repository ABCs; no sleep-based ordering)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from backend.domain.broadcast import BroadcastStation
from backend.domain.streaming import ScheduleItem
from backend.playout.harbor import Upstream
from backend.playout.liquidsoap_process import EngineStartError, RunningEngine, SessionEndpoint
from backend.services.streaming.bookmarks import BookmarkStore
from backend.services.streaming.payload import FinalClip, ItemPayload
from backend.services.streaming.service import (
    ItemCall,
    ListenRequest,
    OpenedStream,
    StreamPorts,
    StreamRepos,
    StreamService,
    StreamServiceConfig,
)
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository
from tests.fakes.playable_schedule import FakePlayableScheduleRepository
from tests.fakes.user_settings import FakeUserSettingRepository
from tests.services.streaming.schedule import (
    BACKEND,
    CALL,
    CLOCK_OFFSET,
    DAY,
    NOW,
    STATION,
    YEAR,
    at,
    sent_path,
    song,
)

__all__ = [
    "BACKEND",
    "CALL",
    "CLOCK_OFFSET",
    "DAY",
    "NOW",
    "STATION",
    "YEAR",
    "Clock",
    "CountingSchedule",
    "EngineStarts",
    "Rig",
    "at",
    "make_rig",
    "sent_path",
    "song",
]


class CountingSchedule(FakePlayableScheduleRepository):
    """The schedule fake, recording every day read (per-session memoisation is checked)."""

    def __init__(self) -> None:
        super().__init__()
        self.loads: list[tuple[UUID, date]] = []

    def get_day(self, station_id: UUID, day: date) -> list[ScheduleItem]:
        self.loads.append((station_id, day))
        return super().get_day(station_id, day)


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@dataclass
class EngineStarts:
    """A fake ``start_engine`` (D1's start_ready_engine, retry included, stands behind it)."""

    fail: bool = False
    chunks: tuple[bytes, ...] = (b"\xff\xfb\x90\x00", b"\x00" * 412)
    endpoints: list[SessionEndpoint] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    upstreams_closed: list[str] = field(default_factory=list)
    stop_seen: asyncio.Event = field(default_factory=asyncio.Event)
    start_seen: asyncio.Event = field(default_factory=asyncio.Event)
    hold: Callable[[], Awaitable[object]] | None = None
    """If set, a start is not ready until ``hold()`` returns (a slow engine start)."""

    async def __call__(self, endpoint: SessionEndpoint) -> RunningEngine:
        self.endpoints.append(endpoint)
        self.start_seen.set()
        if self.hold is not None:
            await self.hold()
        if self.fail:
            raise EngineStartError(f"session {endpoint.session_id}: not ready; retry: not ready")
        pending = list(self.chunks)
        session_id = endpoint.session_id

        async def read() -> bytes:
            return pending.pop(0) if pending else b""

        def stop() -> None:
            self.stopped.append(session_id)
            self.stop_seen.set()

        return RunningEngine(
            pid=40_000 + len(self.endpoints),
            port=endpoint.harbor_port,
            stop=stop,
            upstream=Upstream(read=read, close=lambda: self.upstreams_closed.append(session_id)),
        )

    def token(self, session_id: str) -> str:
        return next(e.session_token for e in self.endpoints if e.session_id == session_id)


@dataclass
class Rig:
    service: StreamService
    stations: FakeBroadcastStationRepository
    schedule: CountingSchedule
    settings: FakeUserSettingRepository
    bookmarks: BookmarkStore
    engines: EngineStarts
    clock: Clock

    async def open_stream(
        self, key: str | None = None, *, call: str = CALL, year: int = YEAR
    ) -> OpenedStream:
        return await self.service.open(
            ListenRequest(call_letters=call, year=year, listener_key=key, icy_metadata=None)
        )

    async def open(self, key: str | None = None, *, call: str = CALL, year: int = YEAR) -> str:
        return (await self.open_stream(key, call=call, year=year)).session_id

    def call(self, session_id: str, seq: int) -> ItemCall:
        return ItemCall(session_id=session_id, token=self.engines.token(session_id), seq=seq)

    async def item(self, session_id: str, seq: int) -> ItemPayload:
        return await self.service.item(self.call(session_id, seq))

    def started(self, session_id: str, seq: int) -> None:
        self.service.started(self.call(session_id, seq))

    def failed(self, session_id: str, seq: int) -> None:
        self.service.failed(self.call(session_id, seq))


def make_rig(
    tmp_path: Path,
    *,
    engine_fails: bool = False,
    settings: dict[str, str] | None = None,
    final_clip: FinalClip | None = None,
) -> Rig:
    stations = FakeBroadcastStationRepository()
    stations.create(BroadcastStation(id=STATION, call_letters=CALL))
    user_settings = FakeUserSettingRepository(settings)
    schedule = CountingSchedule()
    repos = StreamRepos(stations=stations, settings=user_settings, schedule=schedule)

    def open_repos() -> AbstractContextManager[StreamRepos]:
        return nullcontext(repos)

    clock = Clock()
    engines = EngineStarts(fail=engine_fails)
    bookmarks = BookmarkStore()
    ports = StreamPorts(repos=open_repos, start_engine=engines, clock=clock)
    config = StreamServiceConfig(
        callback_base_url=BACKEND, log_dir=tmp_path / "logs", final_clip=final_clip
    )
    service = StreamService(ports, bookmarks, config)
    return Rig(service, stations, schedule, user_settings, bookmarks, engines, clock)
