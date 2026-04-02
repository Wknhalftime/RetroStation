from __future__ import annotations

import json
from typing import Any

import psycopg

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.models import ProgressTracking
from backend.repositories.progress_tracking import ProgressTrackingRepository


class PgProgressTrackingRepository(ProgressTrackingRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> ProgressTracking:
        progress_data = row["progress_data"]
        if isinstance(progress_data, str):
            progress_data = json.loads(progress_data)
        return ProgressTracking(
            task_id=row["task_id"],
            task_type=TaskType(row["task_type"]),
            status=TaskStatus(row["status"]),
            progress_data=progress_data,
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            completed_at=row.get("completed_at"),
        )

    def upsert(self, task: ProgressTracking) -> ProgressTracking:
        self._conn.execute(
            """INSERT INTO progress_tracking
               (task_id, task_type, status, progress_data, started_at, updated_at, completed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (task_id) DO UPDATE SET
                 status = EXCLUDED.status,
                 progress_data = EXCLUDED.progress_data,
                 updated_at = EXCLUDED.updated_at,
                 completed_at = EXCLUDED.completed_at""",
            (task.task_id, task.task_type.value, task.status.value,
             json.dumps(task.progress_data), task.started_at, task.updated_at,
             task.completed_at),
        )
        row = self._conn.execute(
            "SELECT * FROM progress_tracking WHERE task_id = %s", (task.task_id,)
        ).fetchone()
        assert row is not None
        return self._row_to_model(row)

    def get_by_id(self, task_id: str) -> ProgressTracking | None:
        row = self._conn.execute(
            "SELECT * FROM progress_tracking WHERE task_id = %s", (task_id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def list_running(self) -> list[ProgressTracking]:
        rows = self._conn.execute(
            "SELECT * FROM progress_tracking WHERE status = 'running' ORDER BY started_at DESC"
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def mark_stale_as_failed(self, stale_threshold_minutes: int = 10) -> int:
        result = self._conn.execute(
            """UPDATE progress_tracking
               SET status = 'failed'
               WHERE status = 'running'
                 AND updated_at < now() - (interval '1 minute' * %s)""",
            (stale_threshold_minutes,),
        )
        return result.rowcount
