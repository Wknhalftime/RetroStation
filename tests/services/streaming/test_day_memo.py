"""Per-session day memo (carried from PR B: "PR D memoises it per session"; DayLoader contract;
audit: per-play schedule warnings de-duplicated per session)."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from backend.domain.streaming import ScheduleItem
from backend.services.streaming.sessions import memoised_day_loader
from tests.services.streaming.schedule import DAY, song

ITEMS = [song("06:00:00"), song("06:03:20")]


def test_each_day_is_loaded_once() -> None:
    calls: list[date] = []

    def load(on: date) -> Sequence[ScheduleItem]:
        calls.append(on)
        return ITEMS if on == DAY else []

    day_loader = memoised_day_loader(load)
    assert list(day_loader(DAY)) == ITEMS
    assert list(day_loader(DAY)) == ITEMS
    assert list(day_loader(DAY + timedelta(days=1))) == []
    assert calls == [DAY, DAY + timedelta(days=1)]


def test_an_empty_day_is_remembered_too() -> None:
    calls: list[date] = []

    def load(on: date) -> Sequence[ScheduleItem]:
        calls.append(on)
        return []

    day_loader = memoised_day_loader(load)
    day_loader(DAY)
    day_loader(DAY)
    assert calls == [DAY]


def test_concurrent_first_reads_load_the_day_once() -> None:
    calls: list[date] = []
    barrier = threading.Barrier(8)

    def load(on: date) -> Sequence[ScheduleItem]:
        calls.append(on)
        return ITEMS

    day_loader = memoised_day_loader(load)

    def read(_: int) -> Sequence[ScheduleItem]:
        barrier.wait()
        return day_loader(DAY)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(read, range(8)))
    assert calls == [DAY]
    assert all(list(result) == ITEMS for result in results)
