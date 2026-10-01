"""A stop that fails still tells the feed (D78c review, minor M1).

``close`` (and ``open``'s failure cleanup) must tell the channel ``ended`` and forget the
session as still-playing even when the engine's own ``stop()`` raises an unexpected OS
failure. Before this fix, a raising ``stop()`` meant the feed was never told: the session had
already left ``_sessions``, but stayed in the channel's still-playing list forever — a ghost
that blocks the channel from going idle and, if it happened to be the owner, could wrongly
retake the channel on a later hand-back (D78c).

This is a service-boundary regression test for the ghost, not a resumption test, so it builds
its own small rig rather than ``tests.services.streaming.helpers.make_rig``: that helper's
``EngineStarts`` fake never fails to stop, and has no way to ask it to."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

import pytest

from backend.domain.broadcast import BroadcastStation
from backend.playout.harbor import Upstream
from backend.playout.liquidsoap_process import RunningEngine, SessionEndpoint
from backend.services.streaming.bookmarks import BookmarkStore
from backend.services.streaming.service import (
    ListenRequest,
    StreamPorts,
    StreamRepos,
    StreamService,
    StreamServiceConfig,
)
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository
from tests.fakes.user_settings import FakeUserSettingRepository
from tests.services.streaming.helpers import (
    BACKEND,
    CALL,
    DAY,
    STATION,
    YEAR,
    Clock,
    CountingSchedule,
    song,
)

KEY = "car"


async def _unkillable(endpoint: SessionEndpoint) -> RunningEngine:
    """A started engine whose ``stop`` always raises (an unexpected OS failure, not D1's)."""

    async def read() -> bytes:
        return b""

    def stop() -> None:
        raise OSError(f"session {endpoint.session_id}: engine process could not be killed")

    return RunningEngine(
        pid=99_999,
        port=endpoint.harbor_port,
        stop=stop,
        upstream=Upstream(read=read, close=lambda: None),
    )


def _service_with_unkillable_engine(tmp_path: Path) -> StreamService:
    """A StreamService wired like ``helpers.make_rig``'s, except its engine never stops."""
    stations = FakeBroadcastStationRepository()
    stations.create(BroadcastStation(id=STATION, call_letters=CALL))
    schedule = CountingSchedule()
    schedule.set_day(STATION, DAY, [song("06:00:00", title="Fernando", artist="ABBA")])
    repos = StreamRepos(stations=stations, settings=FakeUserSettingRepository(), schedule=schedule)

    def open_repos() -> AbstractContextManager[StreamRepos]:
        return nullcontext(repos)

    ports = StreamPorts(repos=open_repos, start_engine=_unkillable, clock=Clock())
    config = StreamServiceConfig(callback_base_url=BACKEND, log_dir=tmp_path / "logs")
    return StreamService(ports, BookmarkStore(), config)


async def test_a_stop_that_raises_still_leaves_no_channel_behind(tmp_path: Path) -> None:
    # D78c review M1: close pops the session and tells the feed before stopping the engine,
    # so an engine whose stop() raises still frees the feed's hold on this session, and the
    # OSError still propagates (no try/except swallows a genuinely unexpected failure).
    service = _service_with_unkillable_engine(tmp_path)
    opened = await service.open(
        ListenRequest(call_letters=CALL, year=YEAR, listener_key=KEY, icy_metadata=None)
    )
    with pytest.raises(OSError):
        service.close(opened.session_id)
    assert service.event_channels == 0
