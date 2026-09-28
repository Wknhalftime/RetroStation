"""Final-review acceptance tests (PR B): aware ``now`` is rejected at every public entry, and a
resume from a bookmarked item that has since become unplayable counts only the time away."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.domain.streaming import Bookmark, InvalidStreamValueError, ItemRef, Landing
from backend.domain.tune_in import resume
from tests.domain.streaming_helpers import TIMING, D, DictDayLoader, day, now_at

AWARE_NOW = datetime(2026, 3, 14, 6, 1, tzinfo=UTC)


def bookmark_at_start() -> Bookmark:
    return Bookmark(
        Landing(ItemRef(D, 0), 0),
        logged_at=datetime(1995, 3, 14, 6, 0),
        left_at=now_at("06:00:00"),
        clock_offset=timedelta(0),
    )


def test_resume_rejects_aware_now() -> None:
    days = DictDayLoader({D: day(D, [("06:00:00", 240)])})
    with pytest.raises(InvalidStreamValueError, match="now"):
        resume(days, bookmark_at_start(), AWARE_NOW, TIMING)


def test_is_expired_rejects_aware_now() -> None:
    with pytest.raises(InvalidStreamValueError, match="now"):
        bookmark_at_start().is_expired(AWARE_NOW)


def test_resume_from_now_unplayable_item_counts_only_time_away() -> None:
    # Item 0 was playing 200 s in when the listener left; its file has since gone.
    days = DictDayLoader({D: day(D, [("06:00:00", None), ("06:04:00", 240)])})
    left = Bookmark(
        Landing(ItemRef(D, 0), 200_000),
        logged_at=datetime(1995, 3, 14, 6, 0),
        left_at=now_at("12:00:00"),
        clock_offset=timedelta(0),
    )
    assert resume(days, left, now_at("12:00:30"), TIMING) == Landing(ItemRef(D, 1), 30_000)
    assert resume(days, left, now_at("12:00:00"), TIMING) == Landing(ItemRef(D, 1), 0)
