"""StreamService: admission, placement, the item contract, bookmarks, reports and the freeze
watchdog (spec: Service and routes; Errors and edge cases; the contract; D10, D11, D15,
D23-D32, D72; carried: memoised DayLoader, event_id with the committed ItemRef,
EngineStartError translated by the service)."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from backend.domain.broadcast import BroadcastStation
from backend.domain.streaming import (
    Bookmark,
    EndOfScheduleError,
    ItemRef,
    Landing,
    NoBroadcastError,
    ScheduleItem,
)
from backend.domain.system import UserSetting
from backend.playout.liquidsoap_process import EngineStartError, long_path
from backend.services.streaming.bookmarks import BookmarkKey, SavedBookmark
from backend.services.streaming.errors import (
    InvalidStreamSettingError,
    SessionTokenError,
    StationBusyError,
    StreamUnavailableError,
    UnknownItemError,
    UnknownSessionError,
)
from backend.services.streaming.payload import FinalClip
from backend.services.streaming.service import ItemCall
from backend.services.streaming.watchdog import run_freeze_watchdog
from tests.services.streaming.helpers import (
    BACKEND,
    CLOCK_OFFSET,
    DAY,
    NOW,
    STATION,
    YEAR,
    Rig,
    at,
    make_rig,
    sent_path,
    song,
)

KEY = BookmarkKey("car", STATION, YEAR)
EVE, NEW_YEAR = date(1995, 12, 31), date(1996, 1, 1)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return make_rig(tmp_path)


def morning(rig: Rig) -> list[ScheduleItem]:
    """06:00 and 06:03:20 songs, an unresolved play, then 06:07. NOW is 06:01 (60 s in)."""
    items = [
        song("06:00:00", title="Fernando", artist="ABBA"),
        song("06:03:20"),
        song("06:06:40", None),
        song("06:07:00"),
    ]
    rig.schedule.set_day(STATION, DAY, items)
    return items


def gap_morning(rig: Rig) -> list[ScheduleItem]:
    """A logged gap: tuning in at 06:05 lands on the 06:30 song, ahead of the clock (D15)."""
    items = [song("06:00:00"), song("06:30:00"), song("06:33:20")]
    rig.schedule.set_day(STATION, DAY, items)
    rig.clock.now = datetime(2026, 3, 14, 6, 5)
    return items


def new_year(rig: Rig) -> list[ScheduleItem]:
    """New Year's Eve 22:00: the 20:00 song is over, so the 23:00 song plays from the top."""
    items = [
        song("20:00:00", on=EVE),
        song("23:00:00", on=EVE),
        song("00:00:00", on=NEW_YEAR),
        song("00:03:20", on=NEW_YEAR),
    ]
    rig.schedule.set_day(STATION, EVE, items[:2])
    rig.schedule.set_day(STATION, NEW_YEAR, items[2:])
    rig.clock.now = datetime(2025, 12, 31, 22, 0)
    return items


async def leave_after_hearing(rig: Rig, key: str, seconds: float) -> None:
    """Open with ``key``, start the first item, listen ``seconds``, leave."""
    sid = await rig.open(key)
    await rig.item(sid, 0)
    rig.started(sid, 0)
    rig.clock.advance(seconds=seconds)
    rig.service.close(sid)


# ---- admission (D10, D27, D72; Errors: "All slots taken -> 503 station busy") ---------------


async def test_by_default_three_listeners_are_admitted(rig: Rig) -> None:
    morning(rig)
    for _ in range(3):
        await rig.open()
    with pytest.raises(StationBusyError):
        await rig.open()
    assert rig.service.open_sessions == 3


async def test_the_limit_comes_from_the_stream_max_sessions_setting(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, settings={"stream_max_sessions": "1"})
    morning(rig)
    await rig.open()
    with pytest.raises(StationBusyError):
        await rig.open()


async def test_closing_a_session_frees_its_slot_at_once(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, settings={"stream_max_sessions": "1"})
    morning(rig)
    first = await rig.open()
    rig.service.close(first)
    second = await rig.open()
    assert second != first
    assert rig.service.open_sessions == 1


async def test_concurrent_tune_ins_never_exceed_the_limit(tmp_path: Path) -> None:
    # D42: admission is atomic. Both tune-ins read the limit at the same moment, so a gap
    # between checking the limit and taking the slot is always hit.
    rig = make_rig(tmp_path, settings={"stream_max_sessions": "1"})
    morning(rig)
    together = threading.Barrier(2)
    read_setting = rig.settings.get

    def get_together(key: str) -> UserSetting | None:
        with contextlib.suppress(threading.BrokenBarrierError):
            together.wait(timeout=1.0)  # a service that reads it once just waits 1 s
        return read_setting(key)

    rig.settings.get = get_together  # type: ignore[method-assign]
    results = await asyncio.gather(rig.open(), rig.open(), return_exceptions=True)
    assert sum(isinstance(r, StationBusyError) for r in results) == 1
    assert sum(isinstance(r, str) for r in results) == 1
    assert rig.service.open_sessions == 1


async def test_the_limit_counts_every_station(tmp_path: Path) -> None:
    # D42: stream_max_sessions is app-wide, not per station.
    rig = make_rig(tmp_path, settings={"stream_max_sessions": "1"})
    morning(rig)
    other = BroadcastStation(id=uuid4(), call_letters="WLS")
    rig.stations.create(other)
    rig.schedule.set_day(other.id, DAY, [song("06:00:00")])
    await rig.open()
    with pytest.raises(StationBusyError):
        await rig.open(call="WLS")
    assert rig.service.open_sessions == 1


async def test_an_invalid_limit_setting_is_refused_and_logged(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, settings={"stream_max_sessions": "lots"})
    morning(rig)
    with capture_logs() as logs, pytest.raises(InvalidStreamSettingError):
        await rig.open()
    [entry] = [e for e in logs if e["event"] == "stream_setting_invalid"]
    assert entry["log_level"] == "error"
    assert (entry["setting"], entry["value"]) == ("stream_max_sessions", "lots")
    assert rig.engines.endpoints == []


async def test_no_broadcast_frees_the_slot_and_stops_the_engine_it_started(rig: Rig) -> None:
    # R1: the engine starts alongside placement, so a failed placement must stop it.
    with pytest.raises(NoBroadcastError):
        await rig.open()
    assert rig.service.open_sessions == 0
    assert rig.engines.stopped == [e.session_id for e in rig.engines.endpoints]


async def test_the_day_is_read_while_the_engine_starts(rig: Rig) -> None:
    # R1: a cold get_day took 1.6 s on dev; it must overlap the engine start, neither precede
    # nor follow it. The day read cannot finish until the engine start has begun, and the
    # engine start cannot finish until the day read has begun: done one after the other, in
    # either order, the open never completes.
    morning(rig)
    loop = asyncio.get_running_loop()
    read_began = asyncio.Event()
    release = threading.Event()
    read_day = rig.schedule.get_day

    def cold_get_day(station_id: UUID, on: date) -> list[ScheduleItem]:
        loop.call_soon_threadsafe(read_began.set)
        release.wait(timeout=5.0)  # a worker thread: the event loop keeps running
        return read_day(station_id, on)

    rig.schedule.get_day = cold_get_day  # type: ignore[method-assign]
    rig.engines.hold = read_began.wait
    opening = asyncio.create_task(rig.open())
    try:
        await asyncio.wait_for(rig.engines.start_seen.wait(), timeout=2.0)
        assert not opening.done()  # the day is still being read
    finally:
        release.set()
    sid = await asyncio.wait_for(opening, timeout=2.0)
    assert [e.session_id for e in rig.engines.endpoints] == [sid]


# ---- engine start (Errors: "... then 503 unavailable"; D1 does the one retry) --------------


async def test_the_engine_gets_its_callback_url_token_and_log_file(
    rig: Rig, tmp_path: Path
) -> None:
    morning(rig)
    sid = await rig.open()
    [endpoint] = rig.engines.endpoints
    assert endpoint.session_id == sid
    assert endpoint.backend_url == f"{BACKEND}/internal/stream/sessions/{sid}"
    assert len(endpoint.session_token) >= 32
    assert endpoint.log_path == tmp_path / "logs" / f"{sid}.log"


async def test_an_engine_that_cannot_start_is_unavailable_and_frees_the_slot(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path, engine_fails=True)
    morning(rig)
    with pytest.raises(StreamUnavailableError) as raised:
        await rig.open()
    assert not isinstance(raised.value, EngineStartError)
    assert isinstance(raised.value.__cause__, EngineStartError)
    assert rig.service.open_sessions == 0


# ---- the item contract ----------------------------------------------------------------------


async def test_the_first_item_is_the_tune_in_landing_with_its_offset(rig: Rig) -> None:
    items = morning(rig)
    sid = await rig.open()
    first = await rig.item(sid, 0)
    assert first.path == sent_path(items[0])
    assert first.annotations["item_seq"] == "0"
    assert first.annotations["liq_cue_in"] == "60.000"
    assert first.annotations["liq_cue_out"] == "200.000"


async def test_later_items_follow_in_logged_order_skipping_unplayable_plays(rig: Rig) -> None:
    items = morning(rig)
    sid = await rig.open()
    paths = [(await rig.item(sid, seq)).path for seq in range(3)]
    assert paths == [sent_path(items[i]) for i in (0, 1, 3)]
    assert (await rig.item(sid, 1)).annotations["liq_cue_in"] == "0.000"


async def test_the_same_seq_always_returns_the_same_item(rig: Rig) -> None:
    items = morning(rig)
    sid = await rig.open()
    await rig.item(sid, 0)
    once = await rig.item(sid, 1)
    again = await rig.item(sid, 1)
    assert once == again
    assert (await rig.item(sid, 2)).path == sent_path(items[3])


async def test_concurrent_requests_for_one_seq_assign_it_once(rig: Rig) -> None:
    items = morning(rig)
    sid = await rig.open()
    await rig.item(sid, 0)
    a, b = await asyncio.gather(rig.item(sid, 1), rig.item(sid, 1))
    assert a == b
    assert (await rig.item(sid, 2)).path == sent_path(items[3])


async def test_after_the_last_item_the_schedule_ends(rig: Rig) -> None:
    # D39: the next day has no log, so the station signs off (no days are bridged).
    morning(rig)
    sid = await rig.open()
    for seq in range(3):
        await rig.item(sid, seq)
    for _ in range(2):  # the same answer every time
        with pytest.raises(EndOfScheduleError):
            await rig.item(sid, 3)


async def test_with_a_final_clip_it_plays_before_the_end(tmp_path: Path) -> None:
    clip = FinalClip(path=Path("assets/signoff.flac"), span_ms=4_000)
    rig = make_rig(tmp_path, final_clip=clip)
    morning(rig)
    sid = await rig.open()
    for seq in range(3):
        await rig.item(sid, seq)
    final = await rig.item(sid, 3)
    assert final.path == long_path(clip.path)
    assert (final.annotations["final"], final.annotations["liq_cue_out"]) == ("true", "4.000")
    with pytest.raises(EndOfScheduleError):
        await rig.item(sid, 4)


async def test_a_seq_ahead_of_the_next_one_is_unknown(rig: Rig) -> None:
    # Contract: any status other than 200/410 means "retry later"; never 410, never an item.
    morning(rig)
    sid = await rig.open()
    with pytest.raises(UnknownItemError):
        await rig.item(sid, 1)


async def test_item_requests_need_the_session_and_its_token(rig: Rig) -> None:
    morning(rig)
    sid = await rig.open()
    with pytest.raises(UnknownSessionError):
        await rig.service.item(ItemCall(session_id="no-such-session", token="x", seq=0))
    with pytest.raises(SessionTokenError):
        await rig.service.item(ItemCall(session_id=sid, token="wrong", seq=0))
    with pytest.raises(SessionTokenError):
        await rig.service.item(ItemCall(session_id=sid, token=None, seq=0))


@pytest.mark.parametrize("report", ["started", "failed"])
async def test_reports_need_the_sessions_token(rig: Rig, report: str) -> None:
    morning(rig)
    sid = await rig.open()
    await rig.item(sid, 0)
    with pytest.raises(SessionTokenError):
        getattr(rig.service, report)(ItemCall(session_id=sid, token="wrong", seq=0))


# ---- across the new year (D39: a logged next day plays on) ---------------------------------


async def test_the_stream_keeps_playing_into_the_next_year(rig: Rig) -> None:
    items = new_year(rig)
    sid = await rig.open()
    paths = [(await rig.item(sid, seq)).path for seq in range(3)]
    assert paths == [sent_path(items[i]) for i in (1, 2, 3)]


async def test_a_resume_walks_across_the_new_year(rig: Rig) -> None:
    items = new_year(rig)
    await leave_after_hearing(rig, "car", 10)  # 10 s into the 23:00 song; ahead of the clock
    rig.clock.advance(seconds=300)
    second = await rig.open("car")
    landed = await rig.item(second, 0)
    # 10 s + 300 s away: 190 s finish the 23:00 song, 110 s go into the 1996 song.
    assert landed.path == sent_path(items[2])
    assert landed.annotations["liq_cue_in"] == "110.000"


# ---- days are read once per session (carried: memoise; audit: warnings once per session) ---


async def test_each_day_is_read_once_per_session(rig: Rig) -> None:
    morning(rig)
    sid = await rig.open()
    for seq in range(3):
        await rig.item(sid, seq)
    with pytest.raises(EndOfScheduleError):
        await rig.item(sid, 3)
    assert (STATION, DAY) in rig.schedule.loads
    assert len(rig.schedule.loads) == len(set(rig.schedule.loads))


async def test_each_session_reads_its_own_days(rig: Rig) -> None:
    morning(rig)
    await rig.open()
    await rig.open()
    assert rig.schedule.loads.count((STATION, DAY)) == 2


# ---- reports (contract: failed -> "flagged and skipped"; D32) ------------------------------


async def test_a_failed_item_is_flagged_as_a_structured_warning(rig: Rig) -> None:
    items = morning(rig)
    sid = await rig.open()
    await rig.item(sid, 0)
    with capture_logs() as logs:
        rig.failed(sid, 0)
    [entry] = [e for e in logs if e["event"] == "stream_item_failed"]
    assert entry["log_level"] == "warning"
    assert items[0].file is not None
    assert entry["event_id"] == str(items[0].event_id)
    assert entry["file_id"] == str(items[0].file.file_id)
    assert entry["path"] == items[0].file.path


async def test_a_failed_item_does_not_stop_the_next_one(rig: Rig) -> None:
    items = morning(rig)
    sid = await rig.open()
    await rig.item(sid, 0)
    rig.failed(sid, 0)
    assert (await rig.item(sid, 1)).path == sent_path(items[1])


# ---- position and bookmarks (D11, D26, D28, D30) --------------------------------------------


async def test_started_commits_the_position_and_close_bookmarks_it(rig: Rig) -> None:
    items = morning(rig)
    await leave_after_hearing(rig, "car", 30)
    assert rig.bookmarks.get(KEY) == SavedBookmark(
        bookmark=Bookmark(
            landing=Landing(ItemRef(DAY, 0), 90_000),
            logged_at=at("06:00:00"),
            left_at=NOW + timedelta(seconds=30),
            clock_offset=CLOCK_OFFSET,
        ),
        event_id=items[0].event_id,
    )


async def test_the_bookmark_counts_from_the_start_report(rig: Rig) -> None:
    # D11: the time heard is counted from when the item started playing, not from the open.
    morning(rig)
    sid = await rig.open("car")
    await rig.item(sid, 0)
    rig.clock.advance(seconds=3)
    rig.started(sid, 0)
    rig.clock.advance(seconds=30)
    rig.service.close(sid)
    kept = rig.bookmarks.get(KEY)
    assert kept is not None
    assert kept.bookmark.landing == Landing(ItemRef(DAY, 0), 90_000)  # 60 s in + 30 s heard


async def test_a_late_started_report_never_moves_the_position_back(rig: Rig) -> None:
    items = morning(rig)
    sid = await rig.open("car")
    await rig.item(sid, 0)
    await rig.item(sid, 1)
    rig.started(sid, 1)
    rig.clock.advance(seconds=5)
    rig.started(sid, 0)
    rig.service.close(sid)
    kept = rig.bookmarks.get(KEY)
    assert kept is not None
    assert kept.bookmark.landing == Landing(ItemRef(DAY, 1), 5_000)
    assert kept.event_id == items[1].event_id


async def test_leaving_before_the_first_start_bookmarks_the_landing(rig: Rig) -> None:
    items = morning(rig)
    sid = await rig.open("car")
    rig.clock.advance(seconds=4)
    rig.service.close(sid)
    assert rig.bookmarks.get(KEY) == SavedBookmark(
        bookmark=Bookmark(
            landing=Landing(ItemRef(DAY, 0), 60_000),
            logged_at=at("06:00:00"),
            left_at=NOW + timedelta(seconds=4),
            clock_offset=CLOCK_OFFSET,
        ),
        event_id=items[0].event_id,
    )


async def test_no_listener_key_means_no_bookmark(rig: Rig) -> None:
    morning(rig)
    sid = await rig.open()
    await rig.item(sid, 0)
    rig.started(sid, 0)
    rig.service.close(sid)
    assert len(rig.bookmarks) == 0


async def test_reconnecting_resumes_forward_by_the_time_away(rig: Rig) -> None:
    gap_morning(rig)
    await leave_after_hearing(rig, "car", 10)  # the 06:30 song, from the top (D15)
    rig.clock.advance(seconds=60)
    second = await rig.open("car")
    # "the radio kept playing": 10 s heard + 60 s away = 70 s into the 06:30 song (D11)
    assert (await rig.item(second, 0)).annotations["liq_cue_in"] == "70.000"


async def test_an_expired_bookmark_falls_back_to_the_clock(rig: Rig) -> None:
    items = gap_morning(rig)
    await leave_after_hearing(rig, "car", 10)
    rig.clock.now = datetime(2026, 3, 14, 6, 31)  # station 06:31:00 > expires_at 06:30:10
    second = await rig.open("car")
    landed = await rig.item(second, 0)
    assert landed.path == sent_path(items[1])
    assert landed.annotations["liq_cue_in"] == "60.000"  # the clock: 06:31 is 60 s in


async def test_a_bookmark_whose_play_changed_falls_back_to_the_clock(rig: Rig) -> None:
    items = gap_morning(rig)
    await leave_after_hearing(rig, "car", 10)
    rig.schedule.set_day(STATION, DAY, [items[0], song("06:30:00"), items[2]])  # re-logged
    rig.clock.advance(seconds=60)
    second = await rig.open("car")
    # A resume would say 70.000; the clock (06:06:10) lands on the next song from the top.
    assert (await rig.item(second, 0)).annotations["liq_cue_in"] == "0.000"


async def test_a_resume_past_the_end_of_the_log_ends_and_clears_the_bookmark(rig: Rig) -> None:
    # D26 with D39: the walk reached a day with no log, so the station has signed off.
    gap_morning(rig)
    await leave_after_hearing(rig, "car", 10)
    rig.clock.advance(seconds=400)  # past both remaining songs; station 06:11:50 < expiry
    with pytest.raises(EndOfScheduleError):
        await rig.open("car")
    assert rig.bookmarks.get(KEY) is None
    assert rig.service.open_sessions == 0


async def test_the_bookmark_is_cleared_once_the_last_item_has_started(rig: Rig) -> None:
    morning(rig)
    sid = await rig.open("car")
    for seq in range(3):
        await rig.item(sid, seq)
    with pytest.raises(EndOfScheduleError):
        await rig.item(sid, 3)
    for seq in range(3):
        rig.started(sid, seq)
    rig.service.close(sid)
    assert rig.bookmarks.get(KEY) is None


async def test_leaving_before_the_last_item_starts_keeps_the_bookmark(rig: Rig) -> None:
    items = morning(rig)
    sid = await rig.open("car")
    for seq in range(3):
        await rig.item(sid, seq)
    with pytest.raises(EndOfScheduleError):  # Liquidsoap prefetched past the end
        await rig.item(sid, 3)
    rig.started(sid, 0)
    rig.started(sid, 1)
    rig.service.close(sid)
    kept = rig.bookmarks.get(KEY)
    assert kept is not None
    assert kept.event_id == items[1].event_id


# ---- now playing (contract: "starts the now-playing timer (~1 s delay)"; D13, D25) ---------


async def test_the_title_changes_one_second_after_the_item_starts(rig: Rig) -> None:
    morning(rig)
    sid = await rig.open()
    assert rig.service.now_playing(sid) == ""
    await rig.item(sid, 0)
    rig.started(sid, 0)
    rig.clock.advance(seconds=0.5)
    assert rig.service.now_playing(sid) == ""
    rig.clock.advance(seconds=0.5)
    assert rig.service.now_playing(sid) == "ABBA - Fernando"


# ---- freeze watchdog (spec; D31) --------------------------------------------------------------


async def test_a_session_that_never_starts_is_stopped_30_seconds_after_opening(rig: Rig) -> None:
    morning(rig)
    sid = await rig.open()
    rig.clock.advance(seconds=29)
    assert rig.service.frozen_sessions() == []
    rig.clock.advance(seconds=1)
    assert rig.service.frozen_sessions() == [sid]
    rig.service.stop_frozen()
    assert rig.engines.stopped == [sid]
    assert rig.service.frozen_sessions() == []


async def test_a_playing_session_is_stopped_when_the_next_start_is_overdue(rig: Rig) -> None:
    morning(rig)
    sid = await rig.open()
    await rig.item(sid, 0)
    rig.started(sid, 0)
    # 140 s of the landing song remain (200 s span, 60 s in), plus the 30 s grace.
    rig.clock.advance(seconds=169)
    assert rig.service.frozen_sessions() == []
    rig.clock.advance(seconds=1)
    assert rig.service.frozen_sessions() == [sid]


async def test_the_watchdog_timer_stops_frozen_sessions(rig: Rig) -> None:
    morning(rig)
    sid = await rig.open()
    rig.clock.advance(seconds=31)
    watchdog = asyncio.create_task(run_freeze_watchdog(rig.service.stop_frozen, interval_s=0.01))
    try:
        await asyncio.wait_for(rig.engines.stop_seen.wait(), timeout=2.0)
    finally:
        watchdog.cancel()
    assert rig.engines.stopped == [sid]


async def test_every_started_report_also_runs_the_watchdog(rig: Rig) -> None:
    morning(rig)
    stuck = await rig.open()
    playing = await rig.open()
    await rig.item(playing, 0)
    rig.clock.advance(seconds=31)
    rig.started(playing, 0)
    assert rig.engines.stopped == [stuck]


# ---- close ---------------------------------------------------------------------------------


async def test_close_stops_the_engine_once_and_ignores_unknown_sessions(rig: Rig) -> None:
    morning(rig)
    sid = await rig.open()
    rig.service.close(sid)
    rig.service.close(sid)
    rig.service.close("never-opened")
    assert rig.engines.stopped == [sid]
    assert rig.service.open_sessions == 0


async def test_close_all_stops_every_engine(rig: Rig) -> None:
    morning(rig)
    a = await rig.open()
    b = await rig.open()
    rig.service.close_all()
    assert sorted(rig.engines.stopped) == sorted([a, b])
    assert rig.service.open_sessions == 0


async def test_a_closed_session_is_unknown_to_liquidsoap(rig: Rig) -> None:
    morning(rig)
    sid = await rig.open()
    call = rig.call(sid, 0)
    rig.service.close(sid)
    with pytest.raises(UnknownSessionError):
        await rig.service.item(call)
