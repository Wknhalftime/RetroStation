"""What the matching services tell a progress observer (spec 2026-10-17 "Matching run progress").

The services settle pending artists and songs one at a time and call ``tick()`` once per item,
after its result is written. They never log, count or write progress themselves: the observer
(the matching stage's run row, wired in the task) decides what a tick means. ``NO_PROGRESS``
is the default, for callers with no run to report to.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Protocol


class MatchingProgress(Protocol):
    def tick(self) -> None:
        """One pending item was settled (or found decided meanwhile). Never raises."""
        ...


class _NoProgress:
    def tick(self) -> None:
        return None


NO_PROGRESS: MatchingProgress = _NoProgress()


def ticking[T](items: Iterable[T], progress: MatchingProgress) -> Iterator[T]:
    """``items``, ticking ``progress`` once each item's loop body has finished.

    The tick runs when the loop asks for the next item, so it follows the item's writes on
    every path through the body (``continue`` included), and never runs for an item whose
    body raised.
    """
    for item in items:
        yield item
        progress.tick()
