"""Bookmarks: a listener's saved position, and whether it is still worth resuming (D11).

``BookmarkStore`` keys bookmarks by ``(listener_key, station_id, year)`` (D28: no key, no
bookmark). ``bookmark_still_valid`` is the caller's guard before ``resume``: a bookmark is
only worth resuming from if the station clock has not already passed it, and the play it
points at is still the one that was logged there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from backend.domain.streaming import Bookmark, DayLoader

__all__ = ["BookmarkKey", "BookmarkStore", "SavedBookmark", "bookmark_still_valid"]


@dataclass(frozen=True)
class BookmarkKey:
    """One listener's saved place at one station-year."""

    listener_key: str
    station_id: UUID
    year: int


@dataclass(frozen=True)
class SavedBookmark:
    """A bookmark plus the ``event_id`` of the play it landed on.

    Carried from PR B: the event_id travels with the bookmark so a changed log is noticed
    (``bookmark_still_valid`` rejects a bookmark whose play was relogged).
    """

    bookmark: Bookmark
    event_id: UUID
    left_elapsed: datetime | None = field(default=None, compare=False)
    """When the listener left, on the service's elapsed-time clock (D47), so the time away is
    measured on it; ``None`` measures it from ``bookmark.left_at`` on the wall clock. Not
    compared: it is a clock reading, not part of where the listener is."""


class BookmarkStore:
    """Bookmarks kept in memory, one per listener/station/year (spec: BookmarkStore)."""

    def __init__(self) -> None:
        self._saved: dict[BookmarkKey, SavedBookmark] = {}

    def get(self, key: BookmarkKey) -> SavedBookmark | None:
        return self._saved.get(key)

    def put(self, key: BookmarkKey, saved: SavedBookmark, now: datetime) -> None:
        """Save ``saved`` under ``key``.

        ``now`` is currently unused (the audit cut prune-on-put as untraced behaviour); it
        stays in the signature because the locked tests call ``put`` with it.
        """
        self._saved[key] = saved

    def delete(self, key: BookmarkKey) -> None:
        self._saved.pop(key, None)

    def __len__(self) -> int:
        return len(self._saved)


def bookmark_still_valid(load_day: DayLoader, saved: SavedBookmark, now: datetime) -> bool:
    """Whether ``saved`` is still worth resuming from: unexpired, and its play still matches."""
    if saved.bookmark.is_expired(now):
        return False
    ref = saved.bookmark.landing.ref
    items = load_day(ref.day)
    if ref.index >= len(items):
        return False
    return items[ref.index].event_id == saved.event_id
