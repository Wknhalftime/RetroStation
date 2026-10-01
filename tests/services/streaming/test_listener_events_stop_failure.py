"""A stop that fails still tells the feed (D78c review, minors M1 and M2).

``close`` (and ``open``'s failure cleanup) must tell the channel ``ended`` and forget the
session as still-playing even when the engine's own ``stop()`` raises an unexpected OS
failure. Before the M1 fix, a raising ``stop()`` meant the feed was never told: the session
had already left ``_sessions``, but stayed in the channel's still-playing list forever — a
ghost that blocks the channel from going idle and, if it happened to be the owner, could
wrongly retake the channel on a later hand-back (D78c).

M2: on ``open()``'s own failure cleanup, the engine's stop is not the only thing that can go
wrong — admission, placement or the engine's own start usually already has. The error that
failed the open must still be the one the caller sees; a stop that also fails must not bury it.

This is a service-boundary regression test for the ghost and for error precedence, not a
resumption test, so it builds its own small rig rather than
``tests.services.streaming.helpers.make_rig``: that helper's ``EngineStarts`` fake never fails
to stop, and has no way to ask it to."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

import pytest

from backend.domain.broadcast import BroadcastStation
from backend.playout.harbor import Upstream
from backend.playout.liquidsoap_process import RunningEngine, SessionEndpoint
from backend.playout.relay import AsgiApp
from backend.services.streaming.bookmarks import BookmarkStore
from backend.services.streaming.listener_events import Status, StatusKind
from backend.services.streaming.service import (
    EventsRequest,
    ListenRequest,
    StreamPorts,
    StreamRepos,
    StreamService,
    StreamServiceConfig,
)
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository
from tests.fakes.user_settings import FakeUserSettingRepository
from tests.services.streaming.events_rig import drain
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
TUNING = Status(StatusKind.TUNING)
UNAVAILABLE = Status(StatusKind.UNAVAILABLE)


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


def _repos() -> StreamRepos:
    stations = FakeBroadcastStationRepository()
    stations.create(BroadcastStation(id=STATION, call_letters=CALL))
    schedule = CountingSchedule()
    schedule.set_day(STATION, DAY, [song("06:00:00", title="Fernando", artist="ABBA")])
    return StreamRepos(stations=stations, settings=FakeUserSettingRepository(), schedule=schedule)


def _service_with_unkillable_engine(tmp_path: Path) -> StreamService:
    """A StreamService wired like ``helpers.make_rig``'s, except its engine never stops."""
    repos = _repos()

    def open_repos() -> AbstractContextManager[StreamRepos]:
        return nullcontext(repos)

    ports = StreamPorts(repos=open_repos, start_engine=_unkillable, clock=Clock())
    config = StreamServiceConfig(callback_base_url=BACKEND, log_dir=tmp_path / "logs")
    return StreamService(ports, BookmarkStore(), config)


class _RelayFailsAfterTheEngineSettled(StreamService):
    """A ``StreamService`` whose ``_relay_app`` always fails, as its own long-standing comment
    ("an engine settled before ``_relay_app`` raised is halted") says it one day might: no
    current input reaches that line, so a test that pins the cleanup which follows must force
    it here, through the same seam a real future failure would use."""

    def _relay_app(
        self, session_id: str, engine: RunningEngine, icy_metadata: str | None
    ) -> AsgiApp:
        raise RuntimeError("relay construction failed")


def _service_whose_relay_fails_with_an_unkillable_engine(tmp_path: Path) -> StreamService:
    """Like ``_service_with_unkillable_engine``, but the open that follows always fails after
    its engine has settled (D78c review M2)."""
    repos = _repos()

    def open_repos() -> AbstractContextManager[StreamRepos]:
        return nullcontext(repos)

    ports = StreamPorts(repos=open_repos, start_engine=_unkillable, clock=Clock())
    config = StreamServiceConfig(callback_base_url=BACKEND, log_dir=tmp_path / "logs")
    return _RelayFailsAfterTheEngineSettled(ports, BookmarkStore(), config)


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


async def test_a_failed_tune_in_whose_stop_also_raises_tells_once_and_keeps_its_own_error(
    tmp_path: Path,
) -> None:
    # D78c review M2: open()'s own failure cleanup must tell the channel exactly once, let
    # the error that actually failed the open -- not a secondary stop failure -- reach the
    # caller, and still leave no channel behind.
    service = _service_whose_relay_fails_with_an_unkillable_engine(tmp_path)
    events = await service.listener_events(
        EventsRequest(call_letters=CALL, year=YEAR, listener_key=KEY)
    )
    with pytest.raises(RuntimeError, match="relay construction failed"):
        await service.open(
            ListenRequest(call_letters=CALL, year=YEAR, listener_key=KEY, icy_metadata=None)
        )
    assert await drain(events) == [TUNING, UNAVAILABLE]
    await events.aclose()  # the channel goes idle once its one page leaves too
    assert service.event_channels == 0
