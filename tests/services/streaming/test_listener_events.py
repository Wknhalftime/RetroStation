"""The stream service tells the listener feed (spec D13: now-playing backend -> browser, X1;
D73: artist and title only; D74 with Q5: the newest stream on a key is told; D78a: the
stream's status; D78b (provisional): the subscription cap, an unexpected failure is
"unavailable"; D72: any case; D28: no key, no events; D25: the told title is the ICY title;
D47: the root wires the real sleep; contract: "starts the now-playing timer (~1 s delay)";
Errors and edge cases; design note 5: a subscription never reads the schedule)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from backend.config import Settings
from backend.domain.streaming import (
    EndOfScheduleError,
    InvalidStreamValueError,
    NoBroadcastError,
    ScheduleItem,
)
from backend.domain.system import UserSetting
from backend.services.streaming.errors import (
    InvalidStreamSettingError,
    StationBusyError,
    StationNotFoundError,
    StreamUnavailableError,
    SubscriptionLimitError,
)
from backend.services.streaming.listener_events import (
    ListenerEvent,
    ListenerEvents,
    NowPlaying,
    Status,
    StatusKind,
)
from backend.services.streaming.service import EventsRequest
from tests.services.streaming.events_rig import (
    DRAIN_TURNS,
    EventsRig,
    drain,
    make_events_rig,
    next_event,
)
from tests.services.streaming.helpers import DAY, STATION, EngineStarts, song

TUNING = Status(StatusKind.TUNING)
STOPPED = Status(StatusKind.STOPPED)


@pytest.fixture
def rig(tmp_path: Path) -> EventsRig:
    return make_events_rig(tmp_path)


def morning(rig: EventsRig) -> list[ScheduleItem]:
    """06:00 (200 s) and 06:03:20, an unresolved play, then 06:07. NOW is 06:01."""
    items = [
        song("06:00:00", title="Fernando", artist="ABBA"),
        song("06:03:20", title="Waterloo", artist="ABBA"),
        song("06:06:40", None),
        song("06:07:00", title="Rasputin", artist="Boney M."),
    ]
    rig.schedule.set_day(STATION, DAY, items)
    return items


def gap_morning(rig: EventsRig) -> None:
    """A logged gap: tuning in at 06:05 lands on the 06:30 song from the top (D15)."""
    rig.schedule.set_day(STATION, DAY, [song("06:00:00"), song("06:30:00"), song("06:33:20")])
    rig.clock.now = datetime(2026, 3, 14, 6, 5)


async def play_first(rig: EventsRig, key: str | None) -> str:
    """Open with ``key`` and start its first item."""
    sid = await rig.open(key)
    await rig.item(sid, 0)
    rig.started(sid, 0)
    return sid


async def told_title(rig: EventsRig, events: ListenerEvents) -> NowPlaying:
    """The next event, a title, once its delay is released."""
    waiting = asyncio.ensure_future(next_event(events))
    await rig.sleep.wait_pending()
    rig.sleep.release()
    told = await waiting
    assert isinstance(told, NowPlaying)
    return told


# ---- what a listener is told, and when (D13, D78a; contract "~1 s delay") ------------------


async def test_tuning_in_is_told_before_the_first_title(rig: EventsRig) -> None:
    morning(rig)
    events = await rig.subscribe("car")
    await play_first(rig, "car")
    assert await next_event(events) == TUNING
    assert await told_title(rig, events) == NowPlaying(artist="ABBA", title="Fernando")


async def test_the_title_is_told_one_delay_after_the_item_starts(rig: EventsRig) -> None:
    items = morning(rig)
    events = await rig.subscribe("car")
    await play_first(rig, "car")
    assert await next_event(events) == TUNING
    waiting = asyncio.ensure_future(next_event(events))
    await rig.sleep.wait_pending()
    assert not waiting.done()
    assert rig.sleep.asked == [1.0]
    rig.sleep.release()
    assert await waiting == NowPlaying(artist=items[0].artist, title=items[0].title)


async def test_the_told_title_is_the_icy_title(rig: EventsRig) -> None:
    # D25: the SSE title (pushed) and the ICY title (pulled) come from the same `started`
    morning(rig)
    events = await rig.subscribe("car")
    sid = await play_first(rig, "car")
    assert await next_event(events) == TUNING
    told = await told_title(rig, events)
    assert rig.service.now_playing(sid) == f"{told.artist} - {told.title}"


async def test_the_title_delay_is_on_the_steady_clock(rig: EventsRig) -> None:
    # D47: the 1 s delay is measured on the steady clock; a wall-clock jump during it (a DST
    # change, a clock adjustment) neither shows the title early nor holds it back
    morning(rig)
    events = await rig.subscribe("car")
    await play_first(rig, "car")
    assert await next_event(events) == TUNING
    waiting = asyncio.ensure_future(next_event(events))
    await rig.sleep.wait_pending()
    rig.clock.advance(hours=1)  # the wall clock only
    assert await still_waiting(waiting)
    assert rig.sleep.asked == [1.0]
    rig.sleep.release()  # one steady second passes
    assert await waiting == NowPlaying(artist="ABBA", title="Fernando")


async def still_waiting(waiting: asyncio.Future[ListenerEvent]) -> bool:
    """Whether ``waiting`` is still not done after the loop has had ``DRAIN_TURNS`` turns."""
    for _ in range(DRAIN_TURNS):
        await asyncio.sleep(0)
    return not waiting.done()


# ---- a failed tune-in is told why (D78a, D78b; Errors and edge cases; D27, D43) -------------


type Step = Callable[[EventsRig], Awaitable[None]]


async def _nothing(rig: EventsRig) -> None:
    return None


async def _no_song_within_3_hours(rig: EventsRig) -> None:
    rig.schedule.set_day(STATION, DAY, [song("10:00:00")])  # NOW is 06:01


async def _morning(rig: EventsRig) -> None:
    morning(rig)


async def _left_before_the_end(rig: EventsRig) -> None:
    gap_morning(rig)
    sid = await play_first(rig, "car")
    rig.elapse(10)
    rig.service.close(sid)
    rig.elapse(400)  # the radio kept playing past the last song of the log


async def _another_listener(rig: EventsRig) -> None:
    await rig.open()


async def _a_bad_limit(rig: EventsRig) -> None:
    rig.settings.upsert(UserSetting(key="stream_max_sessions", value="lots"))


@dataclass(frozen=True)
class Failure:
    """A tune-in that fails: what is set up before and after subscribing, what ``open``
    raises, and the whole sequence the subscriber is told."""

    before: Step
    after: Step
    raises: type[BaseException]
    told: list[StatusKind]
    settings: dict[str, str] | None = None
    engine_fails: bool = False
    engine_raises: Exception | None = field(default=None)


FAILURES = {
    "no song within 3 h": Failure(
        _no_song_within_3_hours,
        _nothing,
        NoBroadcastError,
        [StatusKind.TUNING, StatusKind.NO_BROADCAST],
    ),
    "resume past the end": Failure(
        _left_before_the_end,
        _nothing,
        EndOfScheduleError,
        [StatusKind.TUNING, StatusKind.NO_BROADCAST],
    ),
    "all slots taken": Failure(
        _morning,
        _another_listener,
        StationBusyError,
        [StatusKind.BUSY],
        settings={"stream_max_sessions": "1"},
    ),
    "engine fails": Failure(
        _morning,
        _nothing,
        StreamUnavailableError,
        [StatusKind.TUNING, StatusKind.UNAVAILABLE],
        engine_fails=True,
    ),
    "bad limit setting": Failure(
        _morning, _a_bad_limit, InvalidStreamSettingError, [StatusKind.UNAVAILABLE]
    ),
    "unexpected error": Failure(
        _morning,
        _nothing,
        RuntimeError,
        [StatusKind.TUNING, StatusKind.UNAVAILABLE],
        engine_raises=RuntimeError("a bug, not a StreamingError"),
    ),
}


@pytest.mark.parametrize("failure", list(FAILURES.values()), ids=list(FAILURES))
async def test_a_failed_tune_in_is_told_why(tmp_path: Path, failure: Failure) -> None:
    rig = make_events_rig(
        tmp_path,
        settings=failure.settings,
        engine_fails=failure.engine_fails,
        engine_raises=failure.engine_raises,
    )
    await failure.before(rig)
    events = await rig.subscribe("car")
    await failure.after(rig)
    with pytest.raises(failure.raises):  # open raises as before (D14's codes stay)
        await rig.open("car")
    assert await drain(events) == [Status(kind) for kind in failure.told]


async def test_the_end_of_the_schedule_is_told_ended(rig: EventsRig) -> None:
    # D78a: "ended" (do not reconnect); D26, D39: the last item has started. Each title's
    # delay passes before the next starts, so each was told in its time.
    items = morning(rig)
    events = await rig.subscribe("car")
    sid = await rig.open("car")
    for seq in range(3):
        await rig.item(sid, seq)
    with pytest.raises(EndOfScheduleError):
        await rig.item(sid, 3)
    for seq in range(3):
        rig.started(sid, seq)
        rig.elapse(200)
    rig.service.close(sid)
    titles = [NowPlaying(artist=i.artist, title=i.title) for i in (items[0], items[1], items[3])]
    assert await drain(events) == [TUNING, *titles, Status(StatusKind.ENDED)]


async def test_closing_the_stream_is_told_stopped(rig: EventsRig) -> None:
    # D78a: "stopped" (the page may reconnect and resume)
    morning(rig)
    events = await rig.subscribe("car")
    sid = await rig.open("car")
    rig.service.close(sid)
    assert await drain(events) == [TUNING, STOPPED]


async def test_a_listener_who_leaves_while_tuning_in_is_told_stopped(rig: EventsRig) -> None:
    # D78a; D6: the listener left mid-start
    morning(rig)
    held = asyncio.Event()
    rig.engines.hold = held.wait
    events = await rig.subscribe("car")
    opening = rig.open_task("car")
    await asyncio.wait_for(rig.engines.start_seen.wait(), timeout=2.0)
    opening.cancel()
    await asyncio.wait({opening})
    assert opening.cancelled()
    assert await drain(events) == [TUNING, STOPPED]


async def test_a_reconnect_with_the_same_key_is_told_on_the_same_subscription(
    rig: EventsRig,
) -> None:
    # D78a with D11: "stopped", then the resumed stream on the same channel
    gap_morning(rig)
    events = await rig.subscribe("car")
    first = await play_first(rig, "car")
    rig.elapse(10)
    rig.service.close(first)
    rig.elapse(60)
    await play_first(rig, "car")
    rig.elapse(1)
    told = await drain(events)
    title = NowPlaying(artist="Artist", title="Title")
    assert told == [TUNING, title, STOPPED, TUNING, title]


async def test_with_two_streams_on_one_key_the_newest_is_told(rig: EventsRig) -> None:
    # D74 with Q5: two tabs share the key; the newest stream owns the channel
    morning(rig)
    events = await rig.subscribe("car")
    older = await rig.open("car")
    newer = await rig.open("car")
    await rig.item(older, 0)
    rig.started(older, 0)
    rig.elapse(1)
    assert await drain(events) == [TUNING, TUNING]
    await rig.item(newer, 0)
    rig.started(newer, 0)
    rig.elapse(1)
    assert await drain(events) == [NowPlaying(artist="ABBA", title="Fernando")]


async def test_subscribing_never_reads_the_schedule(rig: EventsRig) -> None:
    # Design note 5 (coordinator rule): a subscription reads the station and the limit only
    morning(rig)
    first = await rig.subscribe("car")
    assert rig.schedule.loads == []
    await play_first(rig, "car")
    rig.elapse(1)
    read_by_the_stream = list(rig.schedule.loads)
    second = await rig.subscribe("car")
    assert await drain(second) == [NowPlaying(artist="ABBA", title="Fernando")]
    assert len(await drain(first)) == 2
    assert rig.schedule.loads == read_by_the_stream


async def test_unknown_call_letters_have_no_events(rig: EventsRig) -> None:
    # Errors table: unknown call letters are "no broadcast"
    with pytest.raises(StationNotFoundError):
        await rig.subscribe("car", call="KXXX")
    assert rig.service.event_channels == 0


async def test_events_are_found_whatever_the_case(rig: EventsRig) -> None:
    # D72: call letters match in any case, so both reach one channel
    morning(rig)
    events = await rig.subscribe("car", call="kioa")
    await rig.open("car", call="KIOA")
    assert await drain(events) == [TUNING]


async def test_a_keyless_stream_tells_no_one(rig: EventsRig) -> None:
    # D28: no key, no bookmark, no channel
    morning(rig)
    events = await rig.subscribe("car")
    keyless = await play_first(rig, None)
    rig.elapse(1)
    rig.service.close(keyless)
    await rig.open("car")
    assert await next_event(events) == TUNING
    assert await drain(events) == []


@pytest.mark.parametrize(
    ("year", "key", "field_name"),
    [(0, "car", "year"), (10_000, "car", "year"), (1995, "", "listener_key")],
    ids=["year 0", "year 10000", "key empty"],
)
def test_an_events_request_needs_a_calendar_year_and_a_key(
    year: int, key: str, field_name: str
) -> None:
    # D28: no key, no events; the /listen year range
    with pytest.raises(InvalidStreamValueError, match=f"EventsRequest.{field_name}"):
        EventsRequest(call_letters="KIOA", year=year, listener_key=key)


def test_the_root_wires_the_real_sleep() -> None:
    # D47: the delay waits on the loop's monotonic clock; the composition root wires it.
    # Imported here so that a rename in the composition root fails this test alone.
    from backend.main import build_stream_ports

    settings = Settings(_env_file=None)  # type: ignore[call-arg]  # pydantic-settings init arg
    assert build_stream_ports(settings, EngineStarts()).sleep is asyncio.sleep


@pytest.mark.parametrize(
    ("settings", "cap"),
    [({"stream_max_sessions": "1"}, 2), (None, 6)],
    ids=["limit 1", "default 3"],
)
async def test_the_subscription_cap_is_twice_the_listener_cap(
    tmp_path: Path, settings: dict[str, str] | None, cap: int
) -> None:
    # D78b with D42: at most 2 x stream_max_sessions subscriptions, app-wide
    rig = make_events_rig(tmp_path, settings=settings)
    for n in range(cap):
        await rig.subscribe(f"tab-{n}")
    with pytest.raises(SubscriptionLimitError):
        await rig.subscribe("one-more")
