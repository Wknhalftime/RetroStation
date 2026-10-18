from __future__ import annotations

from abc import ABC, abstractmethod


class MatchingRunRepository(ABC):
    """The matching run's one RUNNING ``matching`` progress row (spec 2026-10-17).

    The re-check opens it; each matching stage refreshes it and finishes its stage; the last
    stage closes it. Every write is telemetry: an implementation never raises for a database
    it cannot reach, it drops the write. A row the ``/ws`` reaper failed comes back RUNNING;
    a COMPLETED row and an unknown id are left alone.
    """

    @abstractmethod
    def open(self, run_id: str, *, total: int, stages: int) -> None:
        """Write the RUNNING row: ``{processed: 0, total, stages_left: stages,
        stages_failed: 0}``."""
        ...

    @abstractmethod
    def refresh(self, run_id: str, *, pending: int) -> None:
        """``total = max(total, processed + pending)``, ``processed = total - pending``."""
        ...

    @abstractmethod
    def finish_stage(self, run_id: str, *, pending: int | None, failed: bool) -> None:
        """Count as ``refresh`` does (None keeps ``processed``), then one stage fewer, one
        more failed stage when ``failed``. At no stage left the row is COMPLETED, with
        ``warning = "matching_stages_failed"`` when any stage failed."""
        ...
