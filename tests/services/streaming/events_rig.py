"""The now-playing events rig: the D2 stream service rig (``helpers.py``) with a gated sleep,
so the ~1 s title delay is driven step by step (spec, Testing: "Service: in-memory fakes";
project rule: no sleep-based ordering; contract: "starts the now-playing timer (~1 s
delay)"; D47: the delay is measured on the steady clock, never on the wall clock).

The service is wired to two clocks, as in production (D47): the wall clock (``rig.clock``)
places listeners, and a separate steady clock (``rig.steady``) times everything else.
``rig.elapse()`` is real time passing: both move together. Moving ``rig.clock`` alone is a
wall-clock jump (a DST change or a clock adjustment).

``GatedSleep`` stands in for ``asyncio.sleep``: it records what it was asked and, for a positive
wait, returns only on ``release()``, which first moves the clocks on by that sleep's remaining
time; a wait of 0 s or less returns at once, as ``asyncio.sleep`` does. ``drain`` and
``next_event`` read a subscription without ever waiting on real time: ``drain`` yields to the
loop a bounded number of times, and every wait has a 2 s hang guard that is never used for
ordering.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from backend.domain.broadcast import BroadcastStation
from backend.playout.liquidsoap_process import RunningEngine, SessionEndpoint
from backend.services.streaming.bookmarks import BookmarkStore
from backend.services.streaming.listener_events import ListenerEvent, ListenerEvents
from backend.services.streaming.service import (
    EventsRequest,
    StreamPorts,
    StreamRepos,
    StreamService,
    StreamServiceConfig,
)
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository
from tests.fakes.user_settings import FakeUserSettingRepository
from tests.services.streaming.helpers import Clock, CountingSchedule, EngineStarts, Rig
from tests.services.streaming.schedule import BACKEND, CALL, STATION, YEAR

__all__ = [
    "HANG_GUARD_S",
    "DRAIN_TURNS",
    "STEADY_START",
    "EventsRig",
    "GatedSleep",
    "RaisingEngineStarts",
    "drain",
    "has_ended",
    "make_events_rig",
    "next_event",
]

HANG_GUARD_S = 2.0
"""Fails a test that would otherwise hang; never used to order anything."""

DRAIN_TURNS = 20
"""How many times ``drain`` yields to the loop before it calls a subscription quiet."""

STEADY_EPOCH = datetime(2000, 1, 1)
STEADY_START = STEADY_EPOCH + timedelta(seconds=5_000)
"""The steady clock's first reading: far from the wall clock's 2026, as in production."""


@dataclass
class _Pending:
    target: datetime
    done: asyncio.Future[None]


class GatedSleep:
    """A fake ``sleep``: each call is recorded in ``asked``; a positive one waits for
    ``release()``, and one of 0 s or less returns at once (as ``asyncio.sleep`` does).

    ``clock`` is the clock the sleep is measured on; ``also`` are clocks that move with it (real
    time passing moves the wall clock too). A release takes the oldest pending sleep, moves
    ``clock`` to its target (``max(now, called_at + seconds)``), moves each of ``also`` on by
    the same amount, and lets the sleep return. A sleep that is cancelled while it waits is no
    longer pending.
    """

    def __init__(self, clock: Clock, *also: Clock) -> None:
        self._clock = clock
        self._also = also
        self._waiting: list[_Pending] = []
        self.asked: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.asked.append(seconds)
        if seconds <= 0:
            return
        pending = _Pending(
            self._clock.now + timedelta(seconds=seconds),
            asyncio.get_running_loop().create_future(),
        )
        self._waiting.append(pending)
        try:
            await pending.done
        finally:
            if pending in self._waiting:
                self._waiting.remove(pending)

    @property
    def pending(self) -> int:
        return len(self._waiting)

    def release(self) -> None:
        if not self._waiting:
            raise AssertionError("release() with no sleep pending")
        oldest = self._waiting.pop(0)
        moved = max(timedelta(0), oldest.target - self._clock.now)
        for clock in (self._clock, *self._also):
            clock.now += moved
        oldest.done.set_result(None)

    async def wait_pending(self, count: int = 1) -> None:
        """Yield to the loop until ``count`` sleeps are pending (bounded, then fail)."""
        for _ in range(DRAIN_TURNS):
            if self.pending >= count:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"expected {count} pending sleep(s), found {self.pending}")


@dataclass
class RaisingEngineStarts(EngineStarts):
    """``EngineStarts`` whose start raises ``raises`` (an unexpected failure, not D1's)."""

    raises: Exception | None = None

    async def __call__(self, endpoint: SessionEndpoint) -> RunningEngine:
        if self.raises is not None:
            self.endpoints.append(endpoint)
            self.start_seen.set()
            raise self.raises
        return await super().__call__(endpoint)


@dataclass
class EventsRig(Rig):
    """The D2 rig plus its steady clock and the gated sleep its service was built with."""

    steady: Clock = field(kw_only=True)
    sleep: GatedSleep = field(kw_only=True)

    def elapse(self, seconds: float) -> None:
        """Real time passes: the wall and the steady clock move on together."""
        self.clock.advance(seconds=seconds)
        self.steady.advance(seconds=seconds)

    def open_task(
        self, key: str | None = None, *, call: str = CALL, year: int = YEAR
    ) -> asyncio.Task[str]:
        return asyncio.create_task(self.open(key, call=call, year=year))

    async def subscribe(self, key: str, *, call: str = CALL, year: int = YEAR) -> ListenerEvents:
        return await self.service.listener_events(
            EventsRequest(call_letters=call, year=year, listener_key=key)
        )


def make_events_rig(
    tmp_path: Path,
    *,
    settings: dict[str, str] | None = None,
    engine_fails: bool = False,
    engine_raises: Exception | None = None,
) -> EventsRig:
    """``helpers.make_rig``'s service, wired to a separate steady clock and a ``GatedSleep``
    measured on it (D47)."""
    stations = FakeBroadcastStationRepository()
    stations.create(BroadcastStation(id=STATION, call_letters=CALL))
    user_settings = FakeUserSettingRepository(settings)
    schedule = CountingSchedule()
    repos = StreamRepos(stations=stations, settings=user_settings, schedule=schedule)

    def open_repos() -> AbstractContextManager[StreamRepos]:
        return nullcontext(repos)

    clock, steady = Clock(), Clock(STEADY_START)
    sleep = GatedSleep(steady, clock)
    engines = RaisingEngineStarts(fail=engine_fails, raises=engine_raises)
    bookmarks = BookmarkStore()

    def steady_seconds() -> float:
        return (steady.now - STEADY_EPOCH).total_seconds()

    ports = StreamPorts(
        repos=open_repos, start_engine=engines, clock=clock, steady=steady_seconds, sleep=sleep
    )
    config = StreamServiceConfig(callback_base_url=BACKEND, log_dir=tmp_path / "logs")
    service = StreamService(ports, bookmarks, config)
    return EventsRig(
        service,
        stations,
        schedule,
        user_settings,
        bookmarks,
        engines,
        clock,
        steady=steady,
        sleep=sleep,
    )


class _Ended:
    """The marker ``_step`` returns when the subscription has ended."""


_ENDED = _Ended()


async def _step(events: AsyncIterator[ListenerEvent]) -> ListenerEvent | _Ended:
    try:
        return await anext(events)
    except StopAsyncIteration:
        return _ENDED


async def next_event(events: AsyncIterator[ListenerEvent]) -> ListenerEvent:
    """The next event told; fails if the subscription ends or nothing comes (hang guard)."""
    told = await asyncio.wait_for(_step(events), HANG_GUARD_S)
    if isinstance(told, _Ended):
        raise AssertionError("the subscription ended instead of telling an event")
    return told


async def drain(events: AsyncIterator[ListenerEvent]) -> list[ListenerEvent]:
    """Every event told so far, without waiting on time: stops when the subscription is quiet
    for ``DRAIN_TURNS`` loop turns, or has ended. A pending read is cancelled, and a
    subscription is cancellation-safe, so no event is lost."""
    told: list[ListenerEvent] = []
    while True:
        reading = asyncio.ensure_future(_step(events))
        for _ in range(DRAIN_TURNS):
            if reading.done():
                break
            await asyncio.sleep(0)
        if not reading.done():
            reading.cancel()
            await asyncio.wait({reading})
            return told
        result = reading.result()
        if isinstance(result, _Ended):
            return told
        told.append(result)


async def has_ended(events: AsyncIterator[ListenerEvent]) -> bool:
    """Whether the subscription's iterator has ended (it tells nothing more, ever)."""
    reading = asyncio.ensure_future(_step(events))
    for _ in range(DRAIN_TURNS):
        if reading.done():
            break
        await asyncio.sleep(0)
    if not reading.done():
        reading.cancel()
        await asyncio.wait({reading})
        return False
    return isinstance(reading.result(), _Ended)
