from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.domain.enums import TaskStatus
from backend.domain.system import TaskProgress
from backend.repositories.task_progress import TaskProgressRepository


class FakeTaskProgressRepository(TaskProgressRepository):
    def __init__(self) -> None:
        self._data: dict[str, TaskProgress] = {}
        self.received_upserts: list[TaskProgress] = []
        self.received_touches: list[tuple[str, dict[str, Any]]] = []

    def upsert(self, task: TaskProgress) -> TaskProgress:
        self._data[task.task_id] = task
        self.received_upserts.append(task)
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

    def touch_running(self, task_id: str, progress_overlay: dict[str, Any]) -> int:
        # Mirror the production semantics: shallow-merge the overlay into the
        # existing row's progress_data, refresh updated_at, force status back
        # to RUNNING, and clear completed_at (matches the SQL — see
        # PgTaskProgressRepository.touch_running for the rationale).
        # Returns rowcount (0 when the row doesn't exist).
        self.received_touches.append((task_id, dict(progress_overlay)))
        existing = self._data.get(task_id)
        if existing is None:
            return 0
        merged = {**existing.progress_data, **progress_overlay}
        self._data[task_id] = replace(
            existing,
            status=TaskStatus.RUNNING,
            progress_data=merged,
            updated_at=datetime.now(tz=UTC),
            completed_at=None,
        )
        return 1

