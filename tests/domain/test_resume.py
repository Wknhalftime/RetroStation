"""Task 4 acceptance tests: bookmark resume (spec: D11, D15)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from backend.domain.streaming import Bookmark, EndOfScheduleError, ItemRef, Landing, ScheduleItem
from backend.domain.tune_in import resume, tune_in
from tests.domain.streaming_helpers import (
    NEXT,
    OFFSET_1995,
    TIMING,
    D,
    DictDayLoader,
    day,
    now_at,
)


def gas_pump_schedule() -> dict[date, list[ScheduleItem]]:
    """20 four-minute songs from 14:00, logged back to back."""
    plays = [(f"{14 + m // 60:02d}:{m % 60:02d}:00", 240) for m in range(0, 80, 4)]
    return {D: day(D, plays)}


def test_resume_moves_forward_by_time_away() -> None:
    days = DictDayLoader(gas_pump_schedule())
    left = Bookmark(
        Landing(ItemRef(D, 10), 60_000),
        logged_at=datetime(1995, 3, 14, 14, 40),
        left_at=now_at("14:30:00"),
        clock_offset=OFFSET_1995,
    )
    # Ten minutes away: 60 s + 600 s into song 10 = 2 songs (480 s) + 180 s into song 12.
    assert resume(days, left, now_at("14:40:00"), TIMING) == Landing(ItemRef(D, 12), 180_000)


def test_resume_with_clock_gone_backwards_restarts_at_bookmark() -> None:
    days = DictDayLoader(gas_pump_schedule())
    left = Bookmark(
        Landing(ItemRef(D, 3), 30_000),
        logged_at=datetime(1995, 3, 14, 14, 12),
        left_at=now_at("14:30:00"),
        clock_offset=timedelta(0),
    )
    assert resume(days, left, now_at("14:29:00"), TIMING) == Landing(ItemRef(D, 3), 30_000)


def test_resume_carries_through_gaps_and_ignores_window() -> None:
    days = DictDayLoader(
        {D: day(D, [("22:00:00", 240)]), NEXT: day(NEXT, [("06:00:00", 240), ("06:04:00", 240)])}
    )
    left = Bookmark(
        Landing(ItemRef(D, 0), 60_000),
        logged_at=datetime(1995, 3, 14, 22, 0),
        left_at=now_at("12:00:00"),
        clock_offset=timedelta(0),
    )
    assert resume(days, left, now_at("12:05:00"), TIMING) == Landing(ItemRef(NEXT, 0), 120_000)


def test_resume_past_end_of_schedule_raises() -> None:
    days = DictDayLoader(gas_pump_schedule())
    left = Bookmark(
        Landing(ItemRef(D, 19), 0),
        logged_at=datetime(1995, 3, 14, 15, 16),
        left_at=now_at("14:00:00"),
        clock_offset=timedelta(0),
    )
    with pytest.raises(EndOfScheduleError):
        resume(days, left, now_at("14:30:00"), TIMING)


def test_expired_bookmark_falls_back_to_clock_without_repeats() -> None:
    days = DictDayLoader(gas_pump_schedule())
    # Stream ran 20 minutes ahead: at real 14:30 the listener was in song 12 (logged 14:48).
    left = Bookmark(
        Landing(ItemRef(D, 12), 120_000),
        logged_at=datetime(1995, 3, 14, 14, 48),
        left_at=now_at("14:30:00"),
        clock_offset=OFFSET_1995,
    )
    back = now_at("14:51:00")
    assert left.is_expired(back)
    landing = tune_in(days, 1995, back, TIMING).landing
    assert (landing.ref.index, landing.offset_ms) >= (12, 120_000)
