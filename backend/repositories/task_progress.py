from abc import ABC, abstractmethod

from backend.domain.system import TaskProgress


class TaskProgressRepository(ABC):
    @abstractmethod
    def upsert(self, task: TaskProgress) -> TaskProgress: ...

    @abstractmethod
    def get_by_id(self, task_id: str) -> TaskProgress | None: ...

    @abstractmethod
    def list_running(self) -> list[TaskProgress]: ...

    @abstractmethod
    def mark_stale_as_failed(self, stale_threshold_minutes: int = 10) -> int:
        """Mark running tasks not updated in N minutes as FAILED. Returns count updated."""
        ...

