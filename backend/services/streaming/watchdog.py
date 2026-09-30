"""When a session counts as frozen, and the timer that stops it (D31).

A session is frozen once the clock reaches its deadline: 30 s from open if nothing has
started yet, or the currently playing item's remaining time plus that same grace once
something has. The deadline itself is a pure function; ``StreamService`` decides who is
playing what (Design note: "When a session is dead: watchdog.freeze_deadline(PlayingSpan)").

Every time here is an elapsed-clock reading (D47): the service's steady clock, not the wall
clock. They are comparable only with each other, never with wall or station (domain) times.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

__all__ = ["PlayingSpan", "freeze_deadline", "run_freeze_watchdog"]


@dataclass(frozen=True)
class PlayingSpan:
    """How long the item now playing has been running, and how long it has left.

    ``started_at`` is an elapsed-clock reading (D47), comparable only with other readings of
    that clock, never with the wall clock or the domain's station times.
    """

    started_at: datetime
    remaining_ms: int


def freeze_deadline(opened_at: datetime, playing: PlayingSpan | None, grace: timedelta) -> datetime:
    """The clock time past which a session with no further report counts as frozen.

    Before the first ``started`` report, ``playing`` is ``None`` and the deadline is
    ``grace`` from ``opened_at`` (D31). Once something is playing, the deadline follows it:
    the remaining time in the current item, plus the same grace for the next report.

    ``opened_at``, ``playing.started_at`` and the result are elapsed-clock readings (D47):
    compare the result only with that clock, never with the wall clock or the domain.
    """
    if playing is None:
        return opened_at + grace
    return playing.started_at + timedelta(milliseconds=playing.remaining_ms) + grace


async def run_freeze_watchdog(stop_frozen: Callable[[], None], interval_s: float) -> None:
    """Call ``stop_frozen`` on a timer, forever; the caller cancels the task to stop it."""
    while True:
        stop_frozen()
        await asyncio.sleep(interval_s)
