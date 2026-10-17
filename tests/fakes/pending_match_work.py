"""In-memory fake for the pending-work count (spec 2026-10-17 "Matching run progress")."""

from __future__ import annotations

from backend.repositories.pending_match_work import PendingMatchWork


class FakePendingMatchWork(PendingMatchWork):
    """Answers each read with the next of ``values``; the last one repeats. None is a count
    that failed."""

    def __init__(self, *values: int | None) -> None:
        self._values = list(values) or [0]
        self.reads = 0

    def count_pending(self) -> int | None:
        value = self._values[min(self.reads, len(self._values) - 1)]
        self.reads += 1
        return value
