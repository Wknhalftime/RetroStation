from abc import ABC, abstractmethod

from backend.domain.models import ProgressTracking


class ProgressTrackingRepository(ABC):
    @abstractmethod
    def upsert(self, task: ProgressTracking) -> ProgressTracking: ...

    @abstractmethod
    def get_by_id(self, task_id: str) -> ProgressTracking | None: ...

    @abstractmethod
    def list_running(self) -> list[ProgressTracking]: ...

    @abstractmethod
    def mark_stale_as_failed(self, stale_threshold_minutes: int = 10) -> int:
        """Mark running tasks not updated in N minutes as FAILED. Returns count updated."""
        ...
