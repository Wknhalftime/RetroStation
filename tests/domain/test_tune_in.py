"""Task 2 acceptance tests: clock tune-in and the walk (spec: Domain, D2, D4, D15)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from backend.domain.streaming import (
    CuePoints,
    InvalidStreamValueError,
    ItemRef,
    Landing,
    NoBroadcastError,
    PlayableFile,
    StaleScheduleError,
    StreamTiming,
)
from backend.domain.tune_in import find_anchor, station_wall_clock, tune_in, walk_forward
from tests.domain.streaming_helpers import (
    NEXT,
    OFFSET_1995,
    PREV,
    TIMING,
    D,
    DictDayLoader,
    day,
    now_at,
)

# ---- station wall clock ----


def test_wall_clock_maps_today_onto_station_year() -> None:
    assert station_wall_clock(1995, now_at("14:05:09")) == datetime(1995, 3, 14, 14, 5, 9)


def test_wall_clock_rejects_feb_29_in_non_leap_year() -> None:
    with pytest.raises(NoBroadcastError):
        station_wall_clock(1995, datetime(2028, 2, 29, 12, 0))
    assert station_wall_clock(1996, datetime(2028, 2, 29, 12, 0)) == datetime(1996, 2, 29, 12)


def test_wall_clock_rejects_aware_now() -> None:
    with pytest.raises(InvalidStreamValueError, match="now"):
        station_wall_clock(1995, datetime(2026, 3, 14, 12, 0, tzinfo=UTC))


# ---- anchor ----


def test_anchor_is_inclusive_of_now() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240), ("06:04:00", 240)])})
    assert find_anchor(days, datetime(1995, 3, 14, 6, 4)) == Landing(ItemRef(D, 1), 0)


def test_same_second_plays_anchor_on_the_last_one() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240), ("06:00:00", 240), ("06:10:00", 240)])})
    assert find_anchor(days, datetime(1995, 3, 14, 6, 1)).ref == ItemRef(D, 1)


# ---- tune-in: inside the anchor song ----


def test_lands_mid_song() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240), ("06:04:00", 240)])})
    result = tune_in(days, 1995, now_at("06:02:00"), TIMING)
    assert result.landing == Landing(ItemRef(D, 0), 120_000)
    assert result.clock_offset == OFFSET_1995


def test_tune_in_offset_is_exact_to_the_millisecond() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240)])})
    now = datetime(2026, 3, 14, 6, 0, 1, 1000)
    assert tune_in(days, 1995, now, TIMING).landing == Landing(ItemRef(D, 0), 1001)


def test_short_tail_lands_at_start_of_next_playable() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240), ("06:04:00", None), ("06:04:30", 200)])})
    result = tune_in(days, 1995, now_at("06:03:55"), TIMING)
    assert result.landing == Landing(ItemRef(D, 2), 0)


def test_before_first_play_uses_previous_day_last_play() -> None:
    days = DictDayLoader(
        {
            PREV: day(PREV, [("23:58:00", 240)]),
            D: day(D, [("06:00:00", 240)]),
        }
    )
    result = tune_in(days, 1995, now_at("00:01:00"), TIMING)
    assert result.landing == Landing(ItemRef(PREV, 0), 180_000)


def test_look_back_from_1_march_reaches_29_february() -> None:
    leap, mar1 = date(1996, 2, 29), date(1996, 3, 1)
    days = DictDayLoader(
        {leap: day(leap, [("23:59:00", 240)]), mar1: day(mar1, [("06:00:00", 240)])}
    )
    result = tune_in(days, 1996, datetime(2026, 3, 1, 0, 1), TIMING)
    assert result.landing == Landing(ItemRef(leap, 0), 120_000)


def test_no_previous_day_anchors_on_first_play_within_window() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240)])})
    assert tune_in(days, 1995, now_at("05:00:00"), TIMING).landing == Landing(ItemRef(D, 0), 0)


# ---- tune-in: in a gap, start the song after it (D15) ----


def test_unplayable_anchor_starts_next_song_from_the_top() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", None), ("06:05:00", 240)])})
    result = tune_in(days, 1995, now_at("06:02:00"), TIMING)
    assert result.landing == Landing(ItemRef(D, 1), 0)


def test_log_gap_starts_next_song_from_the_top() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 180), ("07:00:00", 240)])})
    result = tune_in(days, 1995, now_at("06:05:00"), TIMING)
    assert result.landing == Landing(ItemRef(D, 1), 0)


def test_gap_before_midnight_lands_on_next_day() -> None:
    days = DictDayLoader(
        {
            D: day(D, [("23:55:00", 120)]),
            NEXT: day(NEXT, [("00:10:00", 240)]),
        }
    )
    result = tune_in(days, 1995, now_at("23:58:00"), TIMING)
    assert result.landing == Landing(ItemRef(NEXT, 0), 0)


def test_late_night_gap_lands_on_next_day_first_song() -> None:
    days = DictDayLoader(
        {
            D: day(D, [("23:50:00", 240)]),
            NEXT: day(NEXT, [("00:30:00", 240), ("00:34:00", 240)]),
        }
    )
    result = tune_in(days, 1995, now_at("23:59:00"), TIMING)
    assert result.landing == Landing(ItemRef(NEXT, 0), 0)


def test_overnight_gap_lands_on_first_song_after_it() -> None:
    days = DictDayLoader(
        {D: day(D, [("22:00:00", 240)]), NEXT: day(NEXT, [("02:00:00", 240), ("02:04:00", 240)])}
    )
    result = tune_in(days, 1995, now_at("23:59:00"), TIMING)
    assert result.landing == Landing(ItemRef(NEXT, 0), 0)


def test_overnight_gap_beyond_window_is_no_broadcast() -> None:
    days = DictDayLoader({D: day(D, [("22:00:00", 240)]), NEXT: day(NEXT, [("03:00:00", 240)])})
    with pytest.raises(NoBroadcastError):  # 03:00 is 3 h 01 min after 23:59
        tune_in(days, 1995, now_at("23:59:00"), TIMING)


def test_previous_day_ended_lands_on_today_first_song() -> None:
    days = DictDayLoader({PREV: day(PREV, [("18:00:00", 240)]), D: day(D, [("06:00:00", 240)])})
    assert tune_in(days, 1995, now_at("03:30:00"), TIMING).landing == Landing(ItemRef(D, 0), 0)
    with pytest.raises(NoBroadcastError):
        tune_in(days, 1995, now_at("02:59:00"), TIMING)


def test_gap_into_next_year_lands_on_new_year_first_song() -> None:
    eve, new_year = date(1995, 12, 31), date(1996, 1, 1)
    days = DictDayLoader(
        {
            eve: day(eve, [("23:58:00", 60)]),
            new_year: day(new_year, [("00:00:30", 240)]),
        }
    )
    result = tune_in(days, 1995, datetime(2026, 12, 31, 23, 59, 30), TIMING)
    assert result.landing == Landing(ItemRef(new_year, 0), 0)


# ---- tune-in: window and no broadcast (D2) ----


def test_first_play_beyond_window_is_no_broadcast() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240)])})
    with pytest.raises(NoBroadcastError):
        tune_in(days, 1995, now_at("02:59:59"), TIMING)


def test_window_is_inclusive_at_three_hours() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240)])})
    assert tune_in(days, 1995, now_at("03:00:00"), TIMING).landing == Landing(ItemRef(D, 0), 0)


def test_window_applies_to_landing_song_not_anchor() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", None), ("09:30:00", 240)])})
    with pytest.raises(NoBroadcastError):
        tune_in(days, 1995, now_at("06:00:00"), TIMING)


def test_window_is_configurable() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240)])})
    with pytest.raises(NoBroadcastError):
        tune_in(days, 1995, now_at("05:00:00"), StreamTiming(window=timedelta(minutes=30)))


def test_empty_day_is_no_broadcast() -> None:
    with pytest.raises(NoBroadcastError):
        tune_in(DictDayLoader({}), 1995, now_at("06:00:00"), TIMING)


def test_day_with_only_unplayable_plays_is_no_broadcast() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", None), ("06:05:00", 5)])})
    with pytest.raises(NoBroadcastError):
        tune_in(days, 1995, now_at("06:01:00"), TIMING)


def test_missing_next_day_during_tune_in_is_no_broadcast() -> None:
    days = DictDayLoader({D: day(D, [("23:50:00", 240)])})
    with pytest.raises(NoBroadcastError):
        tune_in(days, 1995, now_at("23:59:00"), TIMING)


def test_clock_offset_maps_real_time_to_station_time() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240)])})
    now = now_at("06:01:00")
    assert now + tune_in(days, 1995, now, TIMING).clock_offset == datetime(1995, 3, 14, 6, 1)


# ---- walk: "the radio kept playing" (used by resume) ----


def test_walk_unplayable_items_take_no_time() -> None:
    days = DictDayLoader(
        {D: day(D, [("06:00:00", 240), ("06:04:00", None), ("06:04:30", 5), ("06:05:00", 240)])}
    )
    assert walk_forward(days, Landing(ItemRef(D, 0), 300_000), TIMING) == Landing(
        ItemRef(D, 3), 60_000
    )


def test_walk_offset_equal_to_span_starts_next_song() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240), ("06:04:00", 240)])})
    assert walk_forward(days, Landing(ItemRef(D, 0), 240_000), TIMING) == Landing(ItemRef(D, 1), 0)


def test_walk_short_tail_starts_next_song_from_the_top() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240), ("06:04:00", 240)])})
    assert walk_forward(days, Landing(ItemRef(D, 0), 235_000), TIMING) == Landing(ItemRef(D, 1), 0)


def test_walk_carries_across_gaps_and_days() -> None:
    days = DictDayLoader(
        {D: day(D, [("23:50:00", 240)]), NEXT: day(NEXT, [("06:00:00", 240), ("06:04:00", 240)])}
    )
    assert walk_forward(days, Landing(ItemRef(D, 0), 540_000), TIMING) == Landing(
        ItemRef(NEXT, 1), 60_000
    )


def test_walk_carries_into_next_year() -> None:
    eve, new_year = date(1995, 12, 31), date(1996, 1, 1)
    days = DictDayLoader(
        {eve: day(eve, [("23:58:00", 60)]), new_year: day(new_year, [("00:00:30", 240)])}
    )
    assert walk_forward(days, Landing(ItemRef(eve, 0), 90_000), TIMING) == Landing(
        ItemRef(new_year, 0), 30_000
    )


def test_walk_spends_cue_span_not_file_duration() -> None:
    cued = PlayableFile(
        file_id=uuid4(),
        path="D:/c.flac",
        duration_ms=240_000,
        cues=CuePoints(
            cue_in_ms=2000,
            cue_out_ms=182_000,
            fade_in_ms=3000,
            fade_out_ms=4000,
            start_next_ms=3000,
            gain_db=0.0,
        ),
    )
    items = day(D, [("06:00:00", 240), ("06:03:00", 240)])
    items[0] = replace(items[0], file=cued)
    assert walk_forward(
        DictDayLoader({D: items}), Landing(ItemRef(D, 0), 200_000), TIMING
    ) == Landing(ItemRef(D, 1), 20_000)


def test_walk_stops_at_empty_next_day() -> None:
    days = DictDayLoader(
        {
            D: day(D, [("23:50:00", 240)]),
            NEXT: [],
            date(1995, 3, 16): day(date(1995, 3, 16), [("06:00:00", 240)]),
        }
    )
    assert walk_forward(days, Landing(ItemRef(D, 0), 300_000), TIMING) is None


def test_walk_stops_at_look_ahead_even_when_more_days_exist() -> None:
    days = DictDayLoader(
        {D + timedelta(days=n): day(D + timedelta(days=n), [("06:00:00", 240)]) for n in range(4)}
    )
    start = Landing(ItemRef(D, 0), 3 * 240_000 + 60_000)  # would land 60 s into D+3
    assert walk_forward(days, start, TIMING) is None
    assert walk_forward(days, start, StreamTiming(max_days_ahead=3)) == Landing(
        ItemRef(D + timedelta(days=3), 0), 60_000
    )


def test_walk_from_stale_ref_raises() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240)])})
    with pytest.raises(StaleScheduleError):
        walk_forward(days, Landing(ItemRef(D, 5), 0), TIMING)
