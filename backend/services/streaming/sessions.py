"""Per-session state: a memoised day loader, and the mutable ``StreamSession`` it belongs to.

Every later read of a session's day is served from this memo (carried item C9), so the day
is read at most once per session; the load happens under a lock because session state is
otherwise only ever touched on the event loop (this is the one exception, per the global
constraints: "Only the day memo is touched off the event loop").
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from backend.domain.streaming import DayLoader, ItemRef, ScheduleItem
from backend.playout.liquidsoap_process import RunningEngine

__all__ = ["Assigned", "Committed", "StreamSession", "memoised_day_loader"]


def memoised_day_loader(load: DayLoader) -> DayLoader:
    """Wrap ``load`` so each day is read at most once, even under concurrent first reads."""
    cache: dict[date, Sequence[ScheduleItem]] = {}
    lock = threading.Lock()

    def load_once(day: date) -> Sequence[ScheduleItem]:
        with lock:
            if day not in cache:
                cache[day] = load(day)
            return cache[day]

    return load_once


@dataclass(frozen=True)
class Assigned:
    """What seq was assigned to: the position, its item, and the offset it lands at."""

    ref: ItemRef
    item: ScheduleItem
    offset_ms: int


@dataclass(frozen=True)
class Committed:
    """The position a ``started`` report has confirmed, with when it started."""

    seq: int
    assigned: Assigned
    started_at: datetime


@dataclass
class StreamSession:
    """One listener's mutable state: assignments, the committed position, engine and end.

    ``load_day`` is expected to already be memoised (``memoised_day_loader``). ``assigned``
    maps seq to the item handed out for it, so "the same seq always returns the same item".
    ``now_playing`` is the current now-playing text, once the started delay has elapsed.
    """

    load_day: DayLoader
    assigned: dict[int, Assigned] = field(default_factory=dict)
    committed: Committed | None = None
    end_seq: int | None = None
    engine: RunningEngine | None = None
    now_playing: str = ""
