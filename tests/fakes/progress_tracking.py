from datetime import UTC, datetime, timedelta

from backend.domain.enums import TaskStatus
from backend.domain.models import TaskProgress
from backend.repositories.progress_tracking import TaskProgressRepository


class FakeTaskProgressRepository(TaskProgressRepository):
    def __init__(self) -> None:
        self._data: dict[str, TaskProgress] = {}

    def upsert(self, task: TaskProgress) -> TaskProgress:
        self._data[task.task_id] = task
        return task

    def get_by_id(self, task_id: str) -> TaskProgress | None:
        return self._data.get(task_id)

    def list_running(self) -> list[TaskProgress]:
        return [t for t in self._data.values() if t.status == TaskStatus.RUNNING]

    def mark_stale_as_failed(self, stale_threshold_minutes: int = 10) -> int:
        cutoff = datetime.now(tz=UTC) - timedelta(minutes=stale_threshold_minutes)
        count = 0
        for task in self._data.values():
            if task.status == TaskStatus.RUNNING and task.updated_at < cutoff:
                task.status = TaskStatus.FAILED
                count += 1
        return count
