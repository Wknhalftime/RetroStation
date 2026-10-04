"""A same-key reconnect resumes from the still-open session (spec D109, with D107a; plan
``2026-10-03-tune-in-reconnect-takeover.md`` Revision 2, R1-R10, R12, R13).

The scenario: a phone stalls and reconnects with the same resume key while its old session is
still open (the server notices the dead connection only later, D31), so no bookmark exists yet.
D109: the reconnect lands on the open session's live position (its committed item, at the
offset it started from plus the time since it started, on the steady clock, D47) instead of a
clock tune-in. The old session is not touched (D78c holds).

Where the reconnect lands is read from its engine's first item (seq 0): the path of the file
sent and ``liq_cue_in``. These songs have no cues, so ``liq_cue_in`` is the offset into the
song (D23). Every scenario is built so that the live position, a clock tune-in, a bookmark
resume and "the item from its top" land on different points, so each wrong placement is told
apart. The usual way is an engine that reports its first ``started`` some seconds after the
open: the live position then lags the clock by those seconds.

The service runs on two clocks, as in production (D47): ``rig.elapse()`` is real time passing
and moves both; moving ``rig.clock`` alone is a wall-clock jump.

Judgement calls (recorded for the audit):
- R1 is pinned even when the live position lags the station clock (the engine reported late).
  So treating the live position as a bookmark, which then expires (D11), is wrong under D109
  (coordinator ruling: the live position is where this listener's station is).
- R12: a live position whose play was relogged is not a resume, so it is not logged (R10:
  one log per resume).
- R4 (audit SF-6 ruling): a live open session beats any bookmark on the channel, older or
  newer. The older one is the one the live session itself resumed from; the newer one is
  hand-built, as another tab of the same browser leaves one when it closes (D74 with Q5).
- R10 pins the event name and level only. The session ids are looked for among the event's
  values (as a list, so any field type is allowed), not under particular field names.
  "Nothing else new" is checked against the same scenario where the second request uses
  another key.
- R3: "the newest" is the latest opened (the stream the page follows, D78c), not the latest
  started and not the furthest along (audit MF-1).
- R8: "signed off" means the final item has started (D26), not that the end of the schedule
  is recorded: an engine fetches the sign-off clip while the last song still plays (audit SF-4).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from backend.domain.broadcast import BroadcastStation
from backend.domain.streaming import Bookmark, EndOfScheduleError, ItemRef, Landing, ScheduleItem
from backend.services.streaming.bookmarks import BookmarkKey, SavedBookmark
from backend.services.streaming.errors import StationBusyError
from backend.services.streaming.listener_events import NowPlaying, Status, StatusKind
from backend.services.streaming.payload import FinalClip
from tests.services.streaming.events_rig import EventsRig, drain, make_events_rig
from tests.services.streaming.helpers import CLOCK_OFFSET, DAY, STATION, YEAR, sent_path, song
from tests.services.streaming.sign_off_rig import make_sign_off_rig

RESUMED = "stream_resumed_from_open_session"  # R10: the one new log event
TUNING = Status(StatusKind.TUNING)
FERNANDO = NowPlaying(artist="ABBA", title="Fernando")
WATERLOO = NowPlaying(artist="ABBA", title="Waterloo")

OTHER_CALL = "KXYZ"
OTHER_STATION = UUID("0000c0de-0000-4000-8000-000000000002")
OTHER_YEAR = 1996
OTHER_YEAR_DAY = date(1996, 3, 14)

type Point = tuple[str, str]
"""Where a stream lands: the path sent for its seq 0 and its ``liq_cue_in``."""


@pytest.fixture
def rig(tmp_path: Path) -> EventsRig:
    return make_events_rig(tmp_path)


def morning(rig: EventsRig) -> list[ScheduleItem]:
    """06:00 Fernando, 06:03:20 Waterloo, 06:06:40 Rasputin, 200 s each. NOW is 06:01, so a
    clock tune-in at NOW lands 60 s into Fernando."""
    items = [
        song("06:00:00", title="Fernando", artist="ABBA"),
        song("06:03:20", title="Waterloo", artist="ABBA"),
        song("06:06:40", title="Rasputin", artist="Boney M."),
    ]
    rig.schedule.set_day(STATION, DAY, items)
    return items


def point(item: ScheduleItem, seconds: int) -> Point:
    """``seconds`` into ``item``: no cues, so ``liq_cue_in`` is the offset (D23)."""
    return sent_path(item), f"{seconds}.000"


async def landed(rig: EventsRig, session_id: str) -> Point:
    """Where ``session_id`` landed: its engine's seq 0 request."""
    payload = await rig.item(session_id, 0)
    return payload.path, payload.annotations["liq_cue_in"]


async def playing(
    rig: EventsRig,
    key: str | None,
    *,
    lag: float = 0,
    call: str = "KIOA",
    year: int = YEAR,
) -> str:
    """A stream opens on ``key`` and fetches seq 0; its engine reports it started ``lag``
    seconds later (an engine start or a buffer that takes that long)."""
    session_id = await rig.open(key, call=call, year=year)
    await rig.item(session_id, 0)
    rig.elapse(lag)
    rig.started(session_id, 0)
    return session_id


def resumed(logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [entry for entry in logs if entry.get("event") == RESUMED]


# ---- R1: the live position, never a clock tune-in --------------------------------------------


async def test_a_reconnect_lands_where_the_open_session_is_now(rig: EventsRig) -> None:
    # R1 (D109): the engine reported Fernando started 20 s after the open, at its landing
    # (60 s in); 30 s later it is 90 s in. A clock tune-in now would be 110 s in.
    fernando = morning(rig)[0]
    await playing(rig, "car", lag=20)
    rig.elapse(30)
    reconnect = await rig.open("car")
    assert await landed(rig, reconnect) == point(fernando, 90)


async def test_a_reconnect_follows_the_open_session_onto_its_next_item(rig: EventsRig) -> None:
    # R1: the committed item is the open session's latest started one, from its top. Waterloo
    # started 5 s late, at 06:03:25; 25 s later the station clock would say 30 s.
    waterloo = morning(rig)[1]
    old = await playing(rig, "car")
    rig.elapse(140)
    await rig.item(old, 1)
    rig.elapse(5)
    rig.started(old, 1)
    rig.elapse(25)
    reconnect = await rig.open("car")
    assert await landed(rig, reconnect) == point(waterloo, 25)


async def test_past_the_end_of_its_item_the_reconnect_walks_on_to_the_next(
    rig: EventsRig,
) -> None:
    # R13 (D109, D11): Fernando started 10 s late at 60 s in; 160 s later the open session's
    # point is 220 s, past Fernando's 200 s. Its engine has fetched Waterloo but not reported
    # it started. The reconnect walks on: 20 s into Waterloo (the clock says 30 s).
    waterloo = morning(rig)[1]
    old = await playing(rig, "car", lag=10)
    rig.elapse(160)
    await rig.item(old, 1)
    reconnect = await rig.open("car")
    assert await landed(rig, reconnect) == point(waterloo, 20)


@pytest.mark.parametrize("jump_h", [-1, 1], ids=["clock set back an hour", "clock set on"])
async def test_the_time_since_the_item_started_is_steady_clock_time(
    rig: EventsRig, jump_h: int
) -> None:
    # R1 with D47: 30 s pass on the steady clock while the wall clock jumps an hour (a DST
    # change). The reconnect is 30 s further into Fernando, whatever the wall clock says.
    fernando = morning(rig)[0]
    await playing(rig, "car")
    rig.steady.advance(seconds=30)
    rig.clock.advance(hours=jump_h)
    reconnect = await rig.open("car")
    assert await landed(rig, reconnect) == point(fernando, 90)


# ---- R2: the old session is untouched --------------------------------------------------------


async def test_the_open_session_keeps_playing_and_ends_on_its_own(rig: EventsRig) -> None:
    # R2 (D109, D78c): the old session is still open, its engine runs, it is still served its
    # items, and it ends on its own; the page follows the newest stream.
    fernando, waterloo, _ = morning(rig)
    events = await rig.subscribe("car")
    old = await playing(rig, "car", lag=20)
    rig.elapse(30)
    reconnect = await rig.open("car")
    where = await landed(rig, reconnect)  # its engine fetches seq 0; checked last (R1)
    assert rig.service.open_sessions == 2
    assert rig.service.now_playing(old) == "ABBA - Fernando"  # its own title still shows
    assert old not in rig.engines.stopped
    assert len(rig.service.engine_pids()) == 2
    assert await drain(events) == [TUNING, FERNANDO, TUNING]
    assert (await rig.item(old, 1)).path == sent_path(waterloo)
    rig.started(old, 1)  # still served; not told: the newer stream owns the channel
    rig.elapse(1)
    assert await drain(events) == []
    rig.started(reconnect, 0)
    rig.elapse(1)
    assert await drain(events) == [FERNANDO]
    rig.service.close(old)  # its connection closes in its own time
    assert await drain(events) == []
    assert rig.service.open_sessions == 1
    assert reconnect not in rig.engines.stopped
    assert where == point(fernando, 90)  # it was a resume, so the D109 path ran


async def test_when_the_reconnect_ends_the_open_session_takes_the_channel_back(
    rig: EventsRig,
) -> None:
    # R2 with D78c: the reconnect was resumed from the open session, which still plays; when
    # the reconnect ends, the open session's next title and its close are told again.
    morning(rig)
    events = await rig.subscribe("car")
    old = await playing(rig, "car", lag=20)
    rig.elapse(30)
    reconnect = await playing(rig, "car")
    rig.elapse(1)
    assert await drain(events) == [TUNING, FERNANDO, TUNING, FERNANDO]
    rig.service.close(reconnect)
    assert await drain(events) == [Status(StatusKind.STOPPED)]
    await rig.item(old, 1)
    rig.started(old, 1)
    rig.elapse(1)
    assert await drain(events) == [WATERLOO]
    rig.service.close(old)
    assert await drain(events) == [Status(StatusKind.STOPPED)]


async def test_the_open_sessions_own_position_is_untouched(rig: EventsRig) -> None:
    # R2 (audit SF-3): after the resume, the open session's own position is unchanged. The
    # reconnect plays and leaves (its bookmark, 95 s in, is behind the clock, so it has
    # expired); a third request then lands on the open session's own live point, 110 s in
    # (the clock says 130 s; a position reset at the resume would give 80 s).
    fernando = morning(rig)[0]
    await playing(rig, "car", lag=20)
    rig.elapse(30)
    reconnect = await playing(rig, "car", lag=5)
    rig.elapse(5)
    rig.service.close(reconnect)
    rig.elapse(10)
    third = await rig.open("car")
    assert await landed(rig, third) == point(fernando, 110)


# ---- R3: the newest playing session ----------------------------------------------------------


async def test_with_two_playing_sessions_the_newest_is_used(rig: EventsRig) -> None:
    # R3 (D109 with D78c): the older one is 110 s into Fernando (the clock's point too); the
    # newer one's engine reported 10 s late, so it is 100 s in.
    fernando = morning(rig)[0]
    await playing(rig, "car")
    rig.elapse(20)
    await playing(rig, "car", lag=10)
    rig.elapse(20)
    reconnect = await rig.open("car")
    assert await landed(rig, reconnect) == point(fernando, 100)


async def test_the_newest_is_the_latest_opened_not_the_latest_started(rig: EventsRig) -> None:
    # R3 (D109 "the one the page follows", D78c; audit MF-1): the older stream moved on to
    # Waterloo after the newer one started, so the older has the latest start report and is
    # further along; the newer is still the newest. The newer is 181 s into Fernando, the
    # older 6 s into Waterloo, and the clock says 8 s into Waterloo.
    fernando = morning(rig)[0]
    older = await playing(rig, "car", lag=2)
    rig.elapse(8)
    await playing(rig, "car", lag=25)
    rig.elapse(107)
    await rig.item(older, 1)
    rig.started(older, 1)
    rig.elapse(6)
    reconnect = await rig.open("car")
    assert await landed(rig, reconnect) == point(fernando, 181)


async def test_a_newer_session_that_has_not_started_is_passed_over(rig: EventsRig) -> None:
    # R3 with R7: the newest session that has started playing is used. The newer one never
    # started; the older one is 70 s into Fernando; the clock says 90 s.
    fernando = morning(rig)[0]
    await playing(rig, "car", lag=20)
    not_started = await rig.open("car")
    await rig.item(not_started, 0)
    rig.elapse(10)
    reconnect = await rig.open("car")
    assert await landed(rig, reconnect) == point(fernando, 70)


# ---- R4: a live session beats any bookmark --------------------------------------------------


async def test_a_live_session_beats_the_older_bookmark_it_resumed_from(rig: EventsRig) -> None:
    # R4 (D109, D11): in a logged gap (D15) an earlier stream left a bookmark 10 s into the
    # 06:30 song, ahead of the clock. The live session resumed from it and its engine started
    # 4 s late. The reconnect is 21 s in; the bookmark would give 25 s, the clock 0 s.
    gap = [song("06:00:00"), song("06:30:00"), song("06:33:20")]
    rig.schedule.set_day(STATION, DAY, gap)
    rig.clock.now = datetime(2026, 3, 14, 6, 5)
    earlier = await playing(rig, "car")
    rig.elapse(10)
    rig.service.close(earlier)
    rig.elapse(5)
    await playing(rig, "car", lag=4)
    rig.elapse(6)
    reconnect = await rig.open("car")
    assert await landed(rig, reconnect) == point(gap[1], 21)


async def test_a_live_session_beats_a_bookmark_newer_than_it(rig: EventsRig) -> None:
    # R4 (audit SF-6 ruling: any bookmark, older or newer): after the live session opened, a
    # bookmark 30 s into Waterloo was left on the channel, as a second tab leaves one when it
    # closes. The live session is 70 s into Fernando; the bookmark would give 40 s into
    # Waterloo, the clock 90 s into Fernando.
    fernando, waterloo, _ = morning(rig)
    await playing(rig, "car", lag=20)
    rig.elapse(5)
    left = Bookmark(
        Landing(ItemRef(DAY, 1), 30_000), waterloo.logged_at, rig.clock.now, CLOCK_OFFSET
    )
    rig.bookmarks.put(
        BookmarkKey("car", STATION, YEAR),
        SavedBookmark(left, waterloo.event_id, left_elapsed=rig.steady.now),
        rig.clock.now,
    )
    rig.elapse(5)
    reconnect = await rig.open("car")
    assert await landed(rig, reconnect) == point(fernando, 70)


# ---- R5, R6: other channels ------------------------------------------------------------------


@pytest.mark.parametrize("open_key", ["car", None], ids=["keyed open session", "keyless one"])
async def test_a_keyless_request_tunes_in_by_the_clock(
    rig: EventsRig, open_key: str | None
) -> None:
    # R5 (D28): no key, no resume. The open session is 70 s into Fernando; the clock says 90.
    fernando = morning(rig)[0]
    with capture_logs() as logs:
        await playing(rig, open_key, lag=20)
        rig.elapse(10)
        keyless = await rig.open(None)
        assert await landed(rig, keyless) == point(fernando, 90)
    assert resumed(logs) == []


def other_station(rig: EventsRig) -> ScheduleItem:
    rig.stations.create(BroadcastStation(id=OTHER_STATION, call_letters=OTHER_CALL))
    items = [song("06:00:00", title="Dancing Queen"), song("06:03:20")]
    rig.schedule.set_day(OTHER_STATION, DAY, items)
    return items[0]


def other_year(rig: EventsRig) -> ScheduleItem:
    items = [song("06:00:00", on=OTHER_YEAR_DAY), song("06:03:20", on=OTHER_YEAR_DAY)]
    rig.schedule.set_day(STATION, OTHER_YEAR_DAY, items)
    return items[0]


@pytest.mark.parametrize("elsewhere", ["station", "year"])
async def test_the_same_key_on_another_station_year_is_not_resumed(
    rig: EventsRig, elsewhere: str
) -> None:
    # R6 (D109): the channel is (key, station, year). The open session on KIOA 1995 is 70 s
    # into Fernando; the other station-year's clock says 90 s into its 06:00 song.
    morning(rig)
    first = other_station(rig) if elsewhere == "station" else other_year(rig)
    call, year = (OTHER_CALL, YEAR) if elsewhere == "station" else ("KIOA", OTHER_YEAR)
    with capture_logs() as logs:
        old = await playing(rig, "car", lag=20)
        rig.elapse(10)
        other = await rig.open("car", call=call, year=year)
        assert await landed(rig, other) == point(first, 90)
    assert resumed(logs) == []
    assert rig.service.open_sessions == 2
    assert old not in rig.engines.stopped


@pytest.mark.parametrize("elsewhere", ["station", "year"])
async def test_a_newer_session_elsewhere_does_not_hide_this_channels_session(
    rig: EventsRig, elsewhere: str
) -> None:
    # R6 with R3: a newer stream on the same key plays another station-year. It is not on
    # this channel, so the reconnect still resumes from this channel's own open session, 70 s
    # into Fernando (the clock says 90 s).
    fernando = morning(rig)[0]
    _ = other_station(rig) if elsewhere == "station" else other_year(rig)
    call, year = (OTHER_CALL, YEAR) if elsewhere == "station" else ("KIOA", OTHER_YEAR)
    await playing(rig, "car", lag=20)
    await playing(rig, "car", call=call, year=year)
    rig.elapse(10)
    reconnect = await rig.open("car")
    assert await landed(rig, reconnect) == point(fernando, 70)


# ---- R7, R8: sessions that are not used ------------------------------------------------------


async def test_an_open_session_that_has_not_started_is_not_used(rig: EventsRig) -> None:
    # R7: nothing committed, so the reconnect places as today: by the clock. A logged gap: at
    # 06:05 the 06:30 song plays from its top (D15), and still does 10 s later.
    gap = [song("06:00:00"), song("06:30:00"), song("06:33:20")]
    rig.schedule.set_day(STATION, DAY, gap)
    rig.clock.now = datetime(2026, 3, 14, 6, 5)
    with capture_logs() as logs:
        old = await rig.open("car")
        assert await landed(rig, old) == point(gap[1], 0)
        rig.elapse(10)
        reconnect = await rig.open("car")
        assert await landed(rig, reconnect) == point(gap[1], 0)
    assert resumed(logs) == []


async def test_with_a_session_that_has_not_started_a_bookmark_still_resumes(
    rig: EventsRig,
) -> None:
    # R7 with D11: in a logged gap (D15) an earlier stream played the 06:30 song ahead of the
    # clock and left 10 s in. A new stream resumed from that bookmark (15 s in) and has not
    # started; 5 s later the reconnect resumes from the bookmark too: 20 s in. The clock
    # would play the song from its top.
    gap = [song("06:00:00"), song("06:30:00"), song("06:33:20")]
    rig.schedule.set_day(STATION, DAY, gap)
    rig.clock.now = datetime(2026, 3, 14, 6, 5)
    with capture_logs() as logs:
        earlier = await playing(rig, "car")
        rig.elapse(10)
        rig.service.close(earlier)
        rig.elapse(5)
        not_started = await rig.open("car")
        assert await landed(rig, not_started) == point(gap[1], 15)
        rig.elapse(5)
        reconnect = await rig.open("car")
        assert await landed(rig, reconnect) == point(gap[1], 20)
    assert resumed(logs) == []


async def test_a_session_that_signed_off_is_not_used(rig: EventsRig) -> None:
    # R8 (D26, D78a): the log ends after Waterloo. The open session's Waterloo started 10 s
    # late and the schedule's end was recorded, so it has signed off: the reconnect tunes in by
    # the clock, 30 s into Waterloo (the signed-off session is 20 s in).
    fernando = song("06:00:00", title="Fernando", artist="ABBA")
    waterloo = song("06:03:20", title="Waterloo", artist="ABBA")
    rig.schedule.set_day(STATION, DAY, [fernando, waterloo])
    with capture_logs() as logs:
        old = await playing(rig, "car")
        rig.elapse(140)
        await rig.item(old, 1)
        rig.elapse(10)
        rig.started(old, 1)
        with pytest.raises(EndOfScheduleError):
            await rig.item(old, 2)  # the end of the schedule is recorded (D26)
        rig.elapse(20)
        reconnect = await rig.open("car")
        assert await landed(rig, reconnect) == point(waterloo, 30)
    assert resumed(logs) == []


type Relog = Callable[[list[ScheduleItem]], tuple[list[ScheduleItem], ScheduleItem]]
"""From the morning's items: the day as relogged, and the song a clock tune-in lands in."""


def another_song(items: list[ScheduleItem]) -> tuple[list[ScheduleItem], ScheduleItem]:
    relogged = song("06:00:00", title="Mamma Mia", artist="ABBA")
    return [relogged, *items[1:]], relogged


def same_song_new_play(items: list[ScheduleItem]) -> tuple[list[ScheduleItem], ScheduleItem]:
    """A re-import: the same song, file and time under a new play; only the play id differs."""
    relogged = replace(items[0], event_id=uuid4())
    return [relogged, *items[1:]], relogged


def moved_down(items: list[ScheduleItem]) -> tuple[list[ScheduleItem], ScheduleItem]:
    """A play logged before Fernando: Fernando's play is the same, at the next position."""
    return [song("05:56:40", title="Intro"), *items], items[0]


RELOGS: dict[str, Relog] = {
    "another song": another_song,
    "the same song as a new play": same_song_new_play,
    "the play moved to another position": moved_down,
}


@pytest.mark.parametrize("relog", list(RELOGS.values()), ids=list(RELOGS))
async def test_a_live_position_whose_play_was_relogged_is_not_used(
    rig: EventsRig, relog: Relog
) -> None:
    # R12 (D109 with D11's check; audit SF-1, SF-2): after the open session read its day, the
    # day was relogged. The reconnect reads the day as it is now; the same play is no longer at
    # the open session's position, so it places as today: by the clock, 90 s into the song
    # logged at 06:00 (the open session is 70 s in), and no resume is logged.
    items = morning(rig)
    relogged_day, at_clock = relog(items)
    with capture_logs() as logs:
        await playing(rig, "car", lag=20)
        rig.schedule.set_day(STATION, DAY, relogged_day)
        rig.elapse(10)
        reconnect = await rig.open("car")
        assert await landed(rig, reconnect) == point(at_clock, 90)
    assert resumed(logs) == []


async def test_the_last_song_playing_with_its_clip_fetched_is_still_used(tmp_path: Path) -> None:
    # R8 (D26, PG5; audit SF-4): the last song is playing and the engine has fetched the
    # sign-off clip, so the end of the schedule is recorded, but the clip has not started: the
    # station has not signed off. Waterloo started 5 s late; 10 s later the reconnect is 10 s
    # into Waterloo (the clock says 15 s).
    rig = make_sign_off_rig(
        tmp_path, default_clip=FinalClip(path=tmp_path / "clip.mp3", span_ms=5_000)
    )
    fernando = song("06:00:00", title="Fernando", artist="ABBA")
    waterloo = song("06:03:20", title="Waterloo", artist="ABBA")
    rig.schedule.set_day(STATION, DAY, [fernando, waterloo])
    old = await playing(rig, "car")
    rig.elapse(140)
    await rig.item(old, 1)
    rig.elapse(5)
    rig.started(old, 1)
    assert (await rig.item(old, 2)).annotations["final"] == "true"  # the clip: end recorded
    rig.elapse(10)
    reconnect = await rig.open("car")
    assert await landed(rig, reconnect) == point(waterloo, 10)


# ---- R9: the listener limit is unchanged -----------------------------------------------------


async def test_at_the_limit_a_reconnect_is_busy(tmp_path: Path) -> None:
    # R9 (D42): the reconnect needs a slot of its own; the open session keeps its own.
    rig = make_events_rig(tmp_path, settings={"stream_max_sessions": "1"})
    morning(rig)
    events = await rig.subscribe("car")
    old = await playing(rig, "car", lag=20)
    rig.elapse(10)
    with pytest.raises(StationBusyError):
        await rig.open("car")
    assert rig.service.open_sessions == 1
    assert old not in rig.engines.stopped
    assert Status(StatusKind.BUSY) in await drain(events)


async def test_a_reconnect_counts_toward_the_limit(tmp_path: Path) -> None:
    # R9 (D42): with two slots, the open session and its reconnect take both.
    rig = make_events_rig(tmp_path, settings={"stream_max_sessions": "2"})
    morning(rig)
    await playing(rig, "car", lag=20)
    rig.elapse(10)
    await rig.open("car")
    assert rig.service.open_sessions == 2
    with pytest.raises(StationBusyError):
        await rig.open("bus")
    assert rig.service.open_sessions == 2


# ---- R10: one info log per resume ------------------------------------------------------------


async def reconnect_scenario(rig: EventsRig, reconnect_key: str) -> tuple[str, str]:
    """A stream plays on "car"; a second request comes on ``reconnect_key`` and fetches seq 0."""
    morning(rig)
    old = await playing(rig, "car", lag=20)
    rig.elapse(10)
    second = await rig.open(reconnect_key)
    await rig.item(second, 0)
    return old, second


def kinds(logs: Sequence[Mapping[str, object]]) -> Counter[tuple[object, object]]:
    return Counter((entry.get("event"), entry.get("log_level")) for entry in logs)


async def test_a_resume_is_logged_once_at_info_naming_both_sessions(tmp_path: Path) -> None:
    # R10: one info log naming both session ids; nothing else is logged that the same
    # scenario without a resume (the second request on another key) does not log.
    with capture_logs() as logs:
        old, reconnect = await reconnect_scenario(make_events_rig(tmp_path / "a"), "car")
    with capture_logs() as baseline:
        await reconnect_scenario(make_events_rig(tmp_path / "b"), "bus")
    [entry] = resumed(logs)
    assert entry.get("log_level") == "info"
    values = list(entry.values())
    assert old in values and reconnect in values
    assert kinds(logs) - Counter({(RESUMED, "info"): 1}) == kinds(baseline)
    assert resumed(baseline) == []


async def test_each_resume_is_logged(rig: EventsRig) -> None:
    # R10 with R3: a second reconnect resumes from the first reconnect, now the newest playing
    # session; each resume is logged once, naming its own two sessions.
    morning(rig)
    with capture_logs() as logs:
        old = await playing(rig, "car", lag=20)
        rig.elapse(10)
        first = await playing(rig, "car", lag=5)
        rig.elapse(10)
        second = await rig.open("car")
    entries = resumed(logs)
    assert len(entries) == 2
    first_values, second_values = list(entries[0].values()), list(entries[1].values())
    assert old in first_values and first in first_values
    assert first in second_values and second in second_values
