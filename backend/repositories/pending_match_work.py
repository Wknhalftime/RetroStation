from __future__ import annotations

from abc import ABC, abstractmethod


class PendingMatchWork(ABC):
    """How much matching work is pending: the matching run's progress count (spec 2026-10-17)."""

    @abstractmethod
    def count_pending(self) -> int | None:
        """Pending broadcast artists plus pending broadcast songs. None when the database
        could not answer: the count is telemetry, never a reason to stop matching."""
        ...
