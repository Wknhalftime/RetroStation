from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime


class TaskProgressRetention(ABC):
    """The progress table's retention: finished rows are telemetry, kept only for a while."""

    @abstractmethod
    def delete_finished_before(self, cutoff: datetime) -> int:
        """Delete rows that ended (any status but RUNNING) before *cutoff*; returns the count.

        A row ended at ``completed_at``, or at ``updated_at`` when it has none. The newest
        COMPLETED row of each task type stays whatever its age: it is that type's
        ``last_completed_started_at`` watermark (the matching re-check reads it).
        """
        ...
