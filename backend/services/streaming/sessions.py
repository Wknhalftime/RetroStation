"""Per-session state: a memoised day loader, and the mutable ``StreamSession`` it belongs to.

Every later read of a session's day is served from this memo (carried item C9), so the day
is read at most once per day per session; the load happens under a lock because session
state is otherwise only ever touched on the event loop (this is the one exception, per the
global constraints: "Only the day memo is touched off the event loop").
"""

from __future__ import annotations

import asyncio
import secrets
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from backend.domain.streaming import DayLoader, ItemRef, Landing, ScheduleItem
from backend.playout.liquidsoap_process import RunningEngine
from backend.services.streaming.bookmarks import BookmarkKey
from backend.services.streaming.payload import FinalClip

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
    """The position a ``started`` report has confirmed, with when it started.

    ``assigned`` is a regular item, or the sign-off clip (D26) once the schedule has
    ended with one configured — a clip has no ``ItemRef``/``ScheduleItem`` to offer.
    ``started_at`` is an elapsed-clock reading (D47), comparable only with other readings of
    that clock, never with the wall clock or the domain's station times.
    """

    seq: int
    assigned: Assigned | FinalClip
    started_at: datetime


@dataclass
class StreamSession:
    """One listener's mutable state: no behaviour, just the fields ``StreamService`` reads
    and writes.

    ``load_day`` is expected to already be memoised (``memoised_day_loader``). ``token`` is
    compared, with ``secrets.compare_digest``, against every ``item``/``started``/``failed``
    call. ``bookmark_key`` is ``None`` for a keyless (D28) tune-in. ``landing`` and
    ``clock_offset`` come from placement, which runs after admission (R1), so they start
    ``None`` and are set as soon as placement returns, while the engine may still be
    starting; ``placed`` is set once placement has ended either way, so the engine's first
    ``item`` request (seq 0, often before its start is confirmed) can wait for the landing
    instead of being told to retry. ``assigned`` maps seq to what was handed
    out for it — an item, or the final clip — so "the same seq always returns the same
    item"; ``end_seq`` is the end-of-schedule marker. ``final_failed`` records that the
    engine reported the final clip failed (PG5): it is never committed, since the engine may
    fetch it, and fail it, before the last song starts. ``stopped`` marks a session the
    watchdog (or ``close``) has already stopped, so it is not frozen twice. ``now_playing``
    is ``(shows_at, text)``: the query compares ``shows_at`` to the clock rather than the
    record flipping itself, per CQRS.

    ``opened_at`` and ``now_playing[0]`` (``shows_at``) are elapsed-clock readings (D47), like
    ``Committed.started_at``: comparable only with each other, never with the wall clock or
    the domain's station times.
    """

    load_day: DayLoader
    opened_at: datetime
    bookmark_key: BookmarkKey | None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    landing: Landing | None = None
    clock_offset: timedelta | None = None
    placed: asyncio.Event = field(default_factory=asyncio.Event)
    assigned: dict[int, Assigned | FinalClip] = field(default_factory=dict)
    committed: Committed | None = None
    end_seq: int | None = None
    final_failed: bool = False
    engine: RunningEngine | None = None
    now_playing: tuple[datetime, str] | None = None
    stopped: bool = False
