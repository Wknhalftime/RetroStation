"""A play of the file that just aired is not aired again (Foreplay / Long Time, 2026-11-01).

One recording can be matched to two back-to-back plays. The radio airs it once; a different
song in between lets the file air again. Tune-in, the walk and next-item all follow the rule.
"""

from __future__ import annotations

from datetime import date, datetime, time
from uuid import uuid4

import pytest

from backend.domain.streaming import (
    EndOfScheduleError,
    ItemRef,
    Landing,
    PlayableFile,
    ScheduleItem,
)
from backend.domain.tune_in import next_item, tune_in, walk_forward
from tests.domain.streaming_helpers import NEXT, PREV, TIMING, D, DictDayLoader, now_at, song

type Log = list[tuple[str, PlayableFile | None]]
"""(logged "HH:MM:SS", the file the play resolves to or None); a file is shared by sharing it."""


def plays(on: date, log: Log) -> list[ScheduleItem]:
    return [
        ScheduleItem(
            event_id=uuid4(),
            logged_at=datetime.combine(on, time.fromisoformat(at)),
            title=f"Play at {at}",
            artist="Artist",
            file=file,
        )
        for at, file in log
    ]


# ---- next item ----


def test_next_item_skips_a_play_of_the_file_that_just_aired() -> None:
    a, b = song(240), song(240)
    days = DictDayLoader({D: plays(D, [("06:00:00", a), ("06:03:00", a), ("06:08:00", b)])})
    assert next_item(days, ItemRef(D, 0), TIMING) == ItemRef(D, 2)


def test_next_item_skips_a_whole_run_of_the_same_file() -> None:
    a, b = song(240), song(240)
    log: Log = [("06:00:00", a), ("06:03:00", a), ("06:06:00", a), ("06:09:00", b)]
    days = DictDayLoader({D: plays(D, log)})
    assert next_item(days, ItemRef(D, 0), TIMING) == ItemRef(D, 3)


def test_next_item_skips_the_file_across_a_play_that_does_not_air() -> None:
    a, b = song(240), song(240)
    log: Log = [("06:00:00", a), ("06:03:00", None), ("06:06:00", a), ("06:09:00", b)]
    days = DictDayLoader({D: plays(D, log)})
    assert next_item(days, ItemRef(D, 0), TIMING) == ItemRef(D, 3)


def test_next_item_skips_the_file_across_a_play_too_short_to_air() -> None:
    a, b, short = song(240), song(240), song(5)
    log: Log = [("06:00:00", a), ("06:03:00", short), ("06:06:00", a), ("06:09:00", b)]
    days = DictDayLoader({D: plays(D, log)})
    assert next_item(days, ItemRef(D, 0), TIMING) == ItemRef(D, 3)


def test_next_item_plays_the_file_again_after_a_different_song() -> None:
    a, b = song(240), song(240)
    days = DictDayLoader({D: plays(D, [("06:00:00", a), ("06:04:00", b), ("06:08:00", a)])})
    assert next_item(days, ItemRef(D, 0), TIMING) == ItemRef(D, 1)
    assert next_item(days, ItemRef(D, 1), TIMING) == ItemRef(D, 2)


def test_next_item_skips_the_file_across_midnight() -> None:
    a, b = song(240), song(240)
    days = DictDayLoader(
        {
            D: plays(D, [("23:58:00", a)]),
            NEXT: plays(NEXT, [("00:02:00", a), ("00:06:00", b)]),
        }
    )
    assert next_item(days, ItemRef(D, 0), TIMING) == ItemRef(NEXT, 1)


def test_next_item_ends_when_only_a_repeat_is_left() -> None:
    a = song(240)
    days = DictDayLoader({D: plays(D, [("06:00:00", a), ("06:03:00", a)])})
    with pytest.raises(EndOfScheduleError):
        next_item(days, ItemRef(D, 0), TIMING)


# ---- the walk (resume) ----


def test_walk_skips_a_play_of_the_file_that_just_aired() -> None:
    a, b = song(240), song(240)
    days = DictDayLoader({D: plays(D, [("06:00:00", a), ("06:03:00", a), ("06:08:00", b)])})
    start = Landing(ItemRef(D, 0), 300_000)
    assert walk_forward(days, start, TIMING) == Landing(ItemRef(D, 2), 60_000)


def test_walk_plays_the_file_again_after_a_different_song() -> None:
    a, b = song(240), song(240)
    days = DictDayLoader({D: plays(D, [("06:00:00", a), ("06:04:00", b), ("06:08:00", a)])})
    start = Landing(ItemRef(D, 0), 500_000)
    assert walk_forward(days, start, TIMING) == Landing(ItemRef(D, 2), 20_000)


# ---- tune-in ----


def test_tune_in_near_the_end_of_a_song_skips_its_repeat() -> None:
    a, b = song(240), song(240)
    days = DictDayLoader({D: plays(D, [("06:00:00", a), ("06:04:00", a), ("06:08:00", b)])})
    result = tune_in(days, 1995, now_at("06:03:58"), TIMING)
    assert result.landing == Landing(ItemRef(D, 2), 0)


def test_tune_in_during_the_repeat_lands_inside_the_first_play() -> None:
    # One 400 s recording logged as two plays, 3 minutes apart: the station is still in it.
    a, b = song(400), song(240)
    days = DictDayLoader({D: plays(D, [("06:00:00", a), ("06:03:00", a), ("06:07:00", b)])})
    result = tune_in(days, 1995, now_at("06:04:00"), TIMING)
    assert result.landing == Landing(ItemRef(D, 0), 240_000)


def test_tune_in_during_a_repeat_after_a_play_that_does_not_air() -> None:
    a, b = song(400), song(240)
    log: Log = [("06:00:00", a), ("06:02:00", None), ("06:04:00", a), ("06:08:00", b)]
    days = DictDayLoader({D: plays(D, log)})
    result = tune_in(days, 1995, now_at("06:05:00"), TIMING)
    assert result.landing == Landing(ItemRef(D, 0), 300_000)


def test_tune_in_during_a_repeat_whose_recording_has_ended_starts_the_next_song() -> None:
    a, b = song(200), song(240)
    days = DictDayLoader({D: plays(D, [("06:00:00", a), ("06:03:00", a), ("06:07:00", b)])})
    result = tune_in(days, 1995, now_at("06:03:30"), TIMING)
    assert result.landing == Landing(ItemRef(D, 2), 0)


def test_tune_in_during_a_repeat_after_midnight_lands_in_the_first_play() -> None:
    a, b = song(400), song(240)
    days = DictDayLoader(
        {
            PREV: plays(PREV, [("23:58:00", a)]),
            D: plays(D, [("00:01:00", a), ("00:10:00", b)]),
        }
    )
    result = tune_in(days, 1995, now_at("00:02:00"), TIMING)
    assert result.landing == Landing(ItemRef(PREV, 0), 240_000)


def test_tune_in_during_the_same_file_after_a_different_song_is_unchanged() -> None:
    a, b = song(240), song(240)
    days = DictDayLoader({D: plays(D, [("06:00:00", a), ("06:04:00", b), ("06:08:00", a)])})
    result = tune_in(days, 1995, now_at("06:09:00"), TIMING)
    assert result.landing == Landing(ItemRef(D, 2), 60_000)
