"""Elapsed time uses a steady clock (spec D47, with D11, D30 and D31).

The wall clock (station-local ``datetime.now``) only places a listener on the station's
clock. Everything that measures how long something took runs on a monotonic clock, so a DST
change or a system clock adjustment never distorts it: the freeze watchdog's deadlines (D31),
the 1 s now-playing delay, the time played within an item and the time away between leaving
and resuming (D11).

Every test here moves the two clocks apart: ``elapse`` is real time passing (both clocks
advance together), ``jump_wall`` is a DST change or a clock adjustment (only the wall clock
moves). The one seam these tests name is the steady clock the service is given
(``StreamPorts.steady``): without it the two clocks cannot be moved apart.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from backend.domain.broadcast import BroadcastStation
from backend.domain.streaming import ItemRef, Landing, ScheduleItem
from backend.services.streaming.bookmarks import BookmarkKey, BookmarkStore
from backend.services.streaming.service import (
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
    EngineStarts,
    Rig,
    sent_path,
    song,
)

KEY = BookmarkKey("car", STATION, YEAR)
SPRING_FORWARD, FALL_BACK = 1, -1  # hours the wall clock jumps


@dataclass
class Steady:
    """A monotonic clock in seconds, like ``time.monotonic``: its origin means nothing."""

    seconds: float = 5_000.0

    def __call__(self) -> float:
        return self.seconds


@dataclass
class SteadyRig:
    rig: Rig
    steady: Steady

    def elapse(self, seconds: float) -> None:
        """Real time passes: both clocks move on together."""
        self.rig.clock.advance(seconds=seconds)
        self.steady.seconds += seconds

    def jump_wall(self, hours: int) -> None:
        """A DST change or a clock adjustment: only the wall clock moves."""
        self.rig.clock.advance(hours=hours)


@pytest.fixture
def steady_rig(tmp_path: Path) -> SteadyRig:
    """The D2 rig (helpers.make_rig), with a steady clock beside its wall clock."""
    stations = FakeBroadcastStationRepository()
    stations.create(BroadcastStation(id=STATION, call_letters=CALL))
    settings = FakeUserSettingRepository()
    schedule = CountingSchedule()
    repos = StreamRepos(stations=stations, settings=settings, schedule=schedule)

    def open_repos() -> AbstractContextManager[StreamRepos]:
        return nullcontext(repos)

    wall, steady = Clock(), Steady()
    engines = EngineStarts()
    bookmarks = BookmarkStore()
    ports = StreamPorts(repos=open_repos, start_engine=engines, clock=wall, steady=steady)
    config = StreamServiceConfig(callback_base_url=BACKEND, log_dir=tmp_path / "logs")
    service = StreamService(ports, bookmarks, config)
    return SteadyRig(Rig(service, stations, schedule, settings, bookmarks, engines, wall), steady)


def morning(rig: Rig) -> list[ScheduleItem]:
    """06:00 (200 s) and 06:03:20; the wall clock (NOW) is 06:01, 60 s into the first."""
    items = [song("06:00:00", title="Fernando", artist="ABBA"), song("06:03:20")]
    rig.schedule.set_day(STATION, DAY, items)
    return items


# ---- spring-forward: live sessions are not stopped, bookmarks do not jump an hour ----------


async def test_spring_forward_mid_song_neither_stops_the_session_nor_jumps_the_bookmark(
    steady_rig: SteadyRig,
) -> None:
    # D47 with D31 and D11: 5 s pass while the wall clock jumps an hour ahead.
    rig = steady_rig.rig
    morning(rig)
    sid = await rig.open("car")
    await rig.item(sid, 0)
    rig.started(sid, 0)  # 60 s into the 200 s song: 140 s + 30 s grace remain
    steady_rig.jump_wall(SPRING_FORWARD)
    steady_rig.elapse(5)
    assert rig.service.frozen_sessions() == []
    rig.service.stop_frozen()
    assert rig.engines.stopped == []
    rig.service.close(sid)
    kept = rig.bookmarks.get(KEY)
    assert kept is not None
    assert kept.bookmark.landing == Landing(ItemRef(DAY, 0), 65_000)  # 60 s in + 5 s heard


# ---- fall-back: the watchdog is not blind ---------------------------------------------------


async def test_at_fall_back_a_session_that_never_starts_is_still_stopped_after_30_seconds(
    steady_rig: SteadyRig,
) -> None:
    # D47 with D31: 30 s from session open, however far back the wall clock goes.
    rig = steady_rig.rig
    morning(rig)
    sid = await rig.open()
    steady_rig.jump_wall(FALL_BACK)
    steady_rig.elapse(29)
    assert rig.service.frozen_sessions() == []
    steady_rig.elapse(1)
    assert rig.service.frozen_sessions() == [sid]


async def test_at_fall_back_a_playing_session_is_still_stopped_when_its_next_start_is_overdue(
    steady_rig: SteadyRig,
) -> None:
    # D47 with D31: the rest of the item (140 s) plus the 30 s grace, on the steady clock.
    rig = steady_rig.rig
    morning(rig)
    sid = await rig.open()
    await rig.item(sid, 0)
    rig.started(sid, 0)
    steady_rig.jump_wall(FALL_BACK)
    steady_rig.elapse(169)
    assert rig.service.frozen_sessions() == []
    steady_rig.elapse(1)
    assert rig.service.frozen_sessions() == [sid]


# ---- the 1 s now-playing delay --------------------------------------------------------------


async def test_the_title_still_appears_one_second_after_the_start_when_the_clock_goes_back(
    steady_rig: SteadyRig,
) -> None:
    # D47: the now-playing delay is measured on the steady clock.
    rig = steady_rig.rig
    morning(rig)
    sid = await rig.open()
    await rig.item(sid, 0)
    rig.started(sid, 0)
    steady_rig.jump_wall(FALL_BACK)
    steady_rig.elapse(0.5)
    assert rig.service.now_playing(sid) == ""
    steady_rig.elapse(0.5)
    assert rig.service.now_playing(sid) == "ABBA - Fernando"


# ---- the time away (D11) --------------------------------------------------------------------


@pytest.mark.parametrize("jump", [SPRING_FORWARD, FALL_BACK], ids=["spring", "fall"])
async def test_a_resume_moves_forward_by_the_steady_time_away(
    steady_rig: SteadyRig, jump: int
) -> None:
    # D47 with D11: 10 s heard, then 60 s away while the wall clock jumps an hour.
    # A logged gap puts the landing song (08:00) nearly two hours ahead of the clock (D15), so
    # the bookmark stays unexpired whichever way the wall clock jumps.
    rig = steady_rig.rig
    items = [song("06:00:00"), song("08:00:00"), song("08:03:20")]
    rig.schedule.set_day(STATION, DAY, items)
    rig.clock.now = datetime(2026, 3, 14, 6, 5)
    first = await rig.open("car")
    await rig.item(first, 0)
    rig.started(first, 0)  # the 08:00 song, from the top
    steady_rig.elapse(10)
    rig.service.close(first)
    steady_rig.jump_wall(jump)
    steady_rig.elapse(60)
    second = await rig.open("car")
    landed = await rig.item(second, 0)
    assert landed.path == sent_path(items[1])
    assert landed.annotations["liq_cue_in"] == "70.000"  # 10 s heard + 60 s away


# ---- the wall clock still places the listener (guard against over-correcting) -------------


@pytest.mark.parametrize(
    ("jump", "lands_on"), [(SPRING_FORWARD, 2), (FALL_BACK, 0)], ids=["spring", "fall"]
)
async def test_a_fresh_tune_in_lands_at_the_wall_clocks_time_of_day(
    steady_rig: SteadyRig, jump: int, lands_on: int
) -> None:
    # D47: "the tune-in anchor is today's time of day in the chosen year". The wall clock
    # jumps from 06:01 while no steady time passes; a fresh tune-in follows the wall clock.
    rig = steady_rig.rig
    items = [song("05:00:00"), song("06:00:00"), song("07:00:00")]
    rig.schedule.set_day(STATION, DAY, items)
    steady_rig.jump_wall(jump)
    sid = await rig.open()
    landed = await rig.item(sid, 0)
    assert landed.path == sent_path(items[lands_on])
    assert landed.annotations["liq_cue_in"] == "60.000"
