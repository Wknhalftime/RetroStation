"""Where a listener lands in a station's logged schedule (spec: Domain, D1, D2, D11, D15)."""

from __future__ import annotations

import calendar
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta

from backend.domain.streaming import (
    Bookmark,
    DayLoader,
    EndOfScheduleError,
    InvalidStreamValueError,
    ItemRef,
    Landing,
    NoBroadcastError,
    ScheduleItem,
    StaleScheduleError,
    StreamTiming,
    TuneIn,
    to_ms,
)


def _require_naive_now(where: str, now: datetime) -> None:
    if now.tzinfo is not None:
        raise InvalidStreamValueError(
            f"{where} now must be naive local time, got {now.isoformat()}"
        )


def station_wall_clock(year: int, now: datetime) -> datetime:
    """``now``'s month, day and time in ``year``; 29 Feb of a non-leap year does not exist."""
    _require_naive_now("station_wall_clock", now=now)
    if now.month == 2 and now.day == 29 and not calendar.isleap(year):
        raise NoBroadcastError(f"{year} has no 29 February")
    return datetime.combine(date(year, now.month, now.day), now.time())


def _item_at(load_day: DayLoader, ref: ItemRef) -> ScheduleItem:
    items = load_day(ref.day)
    if ref.index >= len(items):
        raise StaleScheduleError(
            f"{ref.day} #{ref.index} is past the end of a {len(items)}-item day"
        )
    return items[ref.index]


def _last_at_or_before(items: Sequence[ScheduleItem], wall: datetime) -> int | None:
    found: int | None = None
    for index, item in enumerate(items):
        if item.logged_at > wall:
            break
        found = index
    return found


def find_anchor(load_day: DayLoader, wall: datetime) -> Landing:
    """The last play logged at or before ``wall``, offset by the time since it was logged."""
    today = wall.date()
    items = load_day(today)
    if not items:
        raise NoBroadcastError(f"no plays logged on {today}")
    index = _last_at_or_before(items, wall)
    if index is not None:
        return Landing(ItemRef(today, index), to_ms(wall - items[index].logged_at))
    yesterday = today - timedelta(days=1)
    previous = load_day(yesterday)
    if previous:
        last = len(previous) - 1
        return Landing(ItemRef(yesterday, last), to_ms(wall - previous[last].logged_at))
    return Landing(ItemRef(today, 0), 0)


def _items_from(
    load_day: DayLoader, start: ItemRef, days: int
) -> Iterator[tuple[ItemRef, ScheduleItem]]:
    """Items from ``start`` on, over the start day plus ``days`` following days."""
    on, first = start.day, start.index
    for _ in range(days + 1):
        items = load_day(on)
        if not items:
            return
        for index in range(first, len(items)):
            yield ItemRef(on, index), items[index]
        on, first = on + timedelta(days=1), 0


def _next_playable(
    load_day: DayLoader, after: ItemRef, days: int, timing: StreamTiming
) -> ItemRef | None:
    """The first playable item after ``after`` within ``days`` following days."""
    for ref, item in _items_from(load_day, ItemRef(after.day, after.index + 1), days):
        if item.is_playable(timing):
            return ref
    return None


def _land_from_anchor(load_day: DayLoader, anchor: Landing, timing: StreamTiming) -> Landing | None:
    """Inside the anchor song if enough of it is left, else the top of the next song (D15)."""
    file = _item_at(load_day, anchor.ref).file
    if (
        file is not None
        and anchor.offset_ms < file.span_ms()
        and file.tail_fits(anchor.offset_ms, timing)
    ):
        return anchor
    ref = _next_playable(load_day, anchor.ref, timing.max_days_ahead, timing)
    return None if ref is None else Landing(ref, 0)


def walk_forward(load_day: DayLoader, start: Landing, timing: StreamTiming) -> Landing | None:
    """Spend ``start.offset_ms`` of playback from ``start.ref``; None when the schedule ends.

    The "radio kept playing" rule used by resume (D11). Unplayable items take no time. A
    landing needs the minimum span left in the song (``tail_fits``), otherwise the next
    playable item starts from the top.
    """
    _item_at(load_day, start.ref)
    remaining = start.offset_ms
    for ref, item in _items_from(load_day, start.ref, timing.max_days_ahead):
        if item.file is None or not item.file.is_playable(timing):
            continue
        span = item.file.span_ms()
        if remaining < span:
            if item.file.tail_fits(remaining, timing):
                return Landing(ref, remaining)
            remaining = 0
            continue
        remaining -= span
    return None


def tune_in(load_day: DayLoader, year: int, now: datetime, timing: StreamTiming) -> TuneIn:
    """Clock tune-in: land where the station is at ``now`` on this date in ``year``.

    Time since the anchor counts only inside the anchor song; in a logged gap the next
    playable song starts from the top (D15). That song must be logged within
    ``timing.window`` of now (D2).
    """
    wall = station_wall_clock(year, now)
    landing = _land_from_anchor(load_day, find_anchor(load_day, wall), timing)
    if landing is None:
        raise NoBroadcastError(f"nothing playable after {wall.isoformat()}")
    logged_at = _item_at(load_day, landing.ref).logged_at
    if logged_at - wall > timing.window:
        raise NoBroadcastError(
            f"next play at {logged_at.isoformat()} is more than "
            f"{timing.window} after {wall.isoformat()}"
        )
    return TuneIn(landing, wall - now)


def next_item(load_day: DayLoader, after: ItemRef, timing: StreamTiming) -> ItemRef:
    """The next playable item after ``after``, looking at most one day ahead (spec D1)."""
    _item_at(load_day, after)
    ref = _next_playable(load_day, after, 1, timing)
    if ref is None:
        raise EndOfScheduleError(f"no playable item after {after.day} #{after.index}")
    return ref


def resume(load_day: DayLoader, bookmark: Bookmark, now: datetime, timing: StreamTiming) -> Landing:
    """Where the station would be if it had kept playing since ``bookmark.left_at`` (D11).

    If the bookmarked item is no longer playable (its file is gone, or its cues are now
    too short), the stale offset into it is dropped: the walk starts from its top instead,
    so the away time is not also spent on the next song.
    """
    _require_naive_now("resume", now=now)
    start = bookmark.landing
    if not _item_at(load_day, start.ref).is_playable(timing):
        start = Landing(start.ref, 0)
    landing = walk_forward(load_day, start.advanced_by(now - bookmark.left_at), timing)
    if landing is None:
        raise EndOfScheduleError(f"schedule ended while away since {bookmark.left_at}")
    return landing
