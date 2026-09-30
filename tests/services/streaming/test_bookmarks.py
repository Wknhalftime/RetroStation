"""Bookmarks (spec D11; Service "BookmarkStore: bookmarks keyed by (listener_key, station_id,
year)"; carried from PR B: the event_id is stored with the ItemRef so a changed log is noticed)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, timedelta
from uuid import UUID, uuid4

from backend.domain.streaming import Bookmark, ItemRef, Landing, ScheduleItem
from backend.services.streaming.bookmarks import (
    BookmarkKey,
    BookmarkStore,
    SavedBookmark,
    bookmark_still_valid,
)
from tests.services.streaming.schedule import CLOCK_OFFSET, DAY, NOW, STATION, YEAR, at, song

KEY = BookmarkKey("car", STATION, YEAR)


def saved(index: int, logged: str, event_id: UUID, offset_ms: int = 10_000) -> SavedBookmark:
    return SavedBookmark(
        bookmark=Bookmark(
            landing=Landing(ItemRef(DAY, index), offset_ms),
            logged_at=at(logged),
            left_at=NOW,
            clock_offset=CLOCK_OFFSET,
        ),
        event_id=event_id,
    )


def loader(days: dict[date, list[ScheduleItem]]) -> Callable[[date], Sequence[ScheduleItem]]:
    def load(on: date) -> Sequence[ScheduleItem]:
        return days.get(on, [])

    return load


def test_a_bookmark_is_kept_per_listener_station_and_year() -> None:
    store = BookmarkStore()
    mark = saved(1, "06:30:00", uuid4())
    store.put(KEY, mark, NOW)
    assert store.get(KEY) == mark
    assert store.get(BookmarkKey("phone", STATION, YEAR)) is None
    assert store.get(BookmarkKey("car", uuid4(), YEAR)) is None
    assert store.get(BookmarkKey("car", STATION, 1996)) is None


def test_putting_again_replaces_the_bookmark() -> None:
    store = BookmarkStore()
    store.put(KEY, saved(1, "06:30:00", uuid4()), NOW)
    newer = saved(2, "06:33:20", uuid4())
    store.put(KEY, newer, NOW)
    assert store.get(KEY) == newer
    assert len(store) == 1


def test_delete_removes_a_bookmark_and_ignores_a_missing_one() -> None:
    store = BookmarkStore()
    store.put(KEY, saved(1, "06:30:00", uuid4()), NOW)
    store.delete(KEY)
    store.delete(KEY)
    assert store.get(KEY) is None


def test_a_fresh_bookmark_on_the_same_play_is_still_valid() -> None:
    items = [song("06:00:00"), song("06:30:00")]
    mark = saved(1, "06:30:00", items[1].event_id)
    assert bookmark_still_valid(loader({DAY: items}), mark, NOW) is True


def test_an_expired_bookmark_is_not_valid() -> None:
    items = [song("06:00:00"), song("06:30:00")]
    mark = saved(1, "06:30:00", items[1].event_id)
    later = NOW + timedelta(minutes=29, seconds=10)  # station clock 06:30:10 = expires_at
    assert bookmark_still_valid(loader({DAY: items}), mark, later) is False


def test_a_bookmark_past_the_end_of_its_day_is_not_valid() -> None:
    items = [song("06:00:00"), song("06:30:00")]
    mark = saved(1, "06:30:00", items[1].event_id)
    assert bookmark_still_valid(loader({DAY: items[:1]}), mark, NOW) is False


def test_a_bookmark_on_a_different_play_is_not_valid() -> None:
    items = [song("06:00:00"), song("06:30:00")]
    mark = saved(1, "06:30:00", items[1].event_id)
    relogged = [items[0], song("06:30:00")]
    assert bookmark_still_valid(loader({DAY: relogged}), mark, NOW) is False


def test_a_bookmark_whose_day_is_gone_is_not_valid() -> None:
    mark = saved(1, "06:30:00", uuid4())
    assert bookmark_still_valid(loader({}), mark, NOW) is False
