"""Behaviour of the playable schedule fake, so tests and PR D can rely on it."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import partial
from uuid import uuid4

import pytest

from backend.domain.streaming import (
    DayLoader,
    ItemRef,
    Landing,
    PlayableFile,
    ScheduleItem,
    StreamTiming,
    TuneIn,
)
from backend.domain.tune_in import tune_in
from tests.fakes.playable_schedule import FakePlayableScheduleRepository

DAY = date(1995, 3, 14)
STATION = uuid4()


def item(hms: str, span_s: int | None = 200) -> ScheduleItem:
    file = (
        None
        if span_s is None
        else PlayableFile(file_id=uuid4(), path="D:/a.flac", duration_ms=span_s * 1000, cues=None)
    )
    return ScheduleItem(
        event_id=uuid4(),
        logged_at=datetime.combine(DAY, time.fromisoformat(hms)),
        title="T",
        artist="A",
        file=file,
    )


def test_schedule_fake_returns_the_day_set_for_that_station() -> None:
    repo = FakePlayableScheduleRepository()
    items = [item("06:00"), item("06:03", None), item("06:03")]
    repo.set_day(STATION, DAY, items)

    assert repo.get_day(STATION, DAY) == items


def test_schedule_fake_is_empty_for_other_days_and_stations() -> None:
    repo = FakePlayableScheduleRepository()
    repo.set_day(STATION, DAY, [item("06:00")])

    assert repo.get_day(STATION, DAY + timedelta(days=1)) == []
    assert repo.get_day(uuid4(), DAY) == []


def test_schedule_fake_hands_out_copies() -> None:
    repo = FakePlayableScheduleRepository()
    items = [item("06:00")]
    repo.set_day(STATION, DAY, items)

    repo.get_day(STATION, DAY).clear()
    items.clear()

    assert len(repo.get_day(STATION, DAY)) == 1


def test_schedule_fake_rejects_a_day_out_of_logged_order() -> None:
    repo = FakePlayableScheduleRepository()

    with pytest.raises(ValueError, match="logged order"):
        repo.set_day(STATION, DAY, [item("06:05"), item("06:00")])

    assert repo.get_day(STATION, DAY) == []


def test_schedule_fake_serves_tune_in_as_a_day_loader() -> None:
    repo = FakePlayableScheduleRepository()
    repo.set_day(STATION, DAY, [item("06:00"), item("06:05")])
    load_day: DayLoader = partial(repo.get_day, STATION)

    result = tune_in(load_day, 1995, datetime(2026, 3, 14, 6, 1), StreamTiming())

    assert result == TuneIn(
        Landing(ItemRef(DAY, 0), 60_000), datetime(1995, 3, 14) - datetime(2026, 3, 14)
    )
