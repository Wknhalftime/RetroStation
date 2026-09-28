"""Task 3 acceptance tests: the next playable item (spec: D1)."""

from __future__ import annotations

from datetime import date

import pytest

from backend.domain.streaming import EndOfScheduleError, ItemRef, StaleScheduleError
from backend.domain.tune_in import next_item
from tests.domain.streaming_helpers import NEXT, TIMING, D, DictDayLoader, day


def test_next_item_skips_unplayable_plays() -> None:
    days = DictDayLoader(
        {D: day(D, [("06:00:00", 240), ("06:04:00", None), ("06:05:00", 5), ("06:06:00", 200)])}
    )
    assert next_item(days, ItemRef(D, 0), TIMING) == ItemRef(D, 3)


def test_next_item_ignores_the_clock() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240), ("20:00:00", 240)])})
    assert next_item(days, ItemRef(D, 0), TIMING) == ItemRef(D, 1)


def test_next_item_rolls_into_next_day() -> None:
    days = DictDayLoader({D: day(D, [("23:58:00", 240)]), NEXT: day(NEXT, [("00:02:00", 240)])})
    assert next_item(days, ItemRef(D, 0), TIMING) == ItemRef(NEXT, 0)


def test_next_item_ends_when_next_day_missing() -> None:
    days = DictDayLoader({D: day(D, [("23:58:00", 240)])})
    with pytest.raises(EndOfScheduleError):
        next_item(days, ItemRef(D, 0), TIMING)


def test_next_item_ends_when_next_day_has_nothing_playable() -> None:
    days = DictDayLoader(
        {
            D: day(D, [("23:58:00", 240)]),
            NEXT: day(NEXT, [("00:02:00", None)]),
            date(1995, 3, 16): day(date(1995, 3, 16), [("06:00:00", 240)]),
        }
    )
    with pytest.raises(EndOfScheduleError):
        next_item(days, ItemRef(D, 0), TIMING)


def test_next_item_after_stale_ref_raises() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240)])})
    with pytest.raises(StaleScheduleError):
        next_item(days, ItemRef(D, 5), TIMING)
