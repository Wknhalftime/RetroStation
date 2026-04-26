from __future__ import annotations

import json
from typing import Any

import psycopg

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.repositories.task_progress import TaskProgressRepository


class PgTaskProgressRepository(TaskProgressRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> TaskProgress:
        progress_data = row["progress_data"]
        if isinstance(progress_data, str):
            progress_data = json.loads(progress_data)
        return TaskProgress(
            task_id=row["task_id"],
            task_type=TaskType(row["task_type"]),
            status=TaskStatus(row["status"]),
            progress_data=progress_data,
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            completed_at=row.get("completed_at"),
        )

    def upsert(self, task: TaskProgress) -> TaskProgress:
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
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_id(self, task_id: str) -> TaskProgress | None:
        row = self._conn.execute(
            "SELECT * FROM progress_tracking WHERE task_id = %s", (task_id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def list_running(self) -> list[TaskProgress]:
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

    def touch_running(self, task_id: str, progress_overlay: dict[str, Any]) -> int:
        # Single round-trip: UPDATE only, no follow-up SELECT. Server-side
        # `now()` keeps `updated_at` anchored to the same clock the WS
        # stale-cleanup query uses. Deliberately no `AND status='running'`
        # guard: a heartbeat must be able to resurrect a row that the WS
        # stale-cleanup tentatively flipped to `failed` while the worker
        # was busy in a long pre-pass — that resurrection is the contract
        # this method exists to provide.
        #
        # `completed_at = NULL` is part of the resurrection: the WS
        # stale-cleanup sets `completed_at = now()` when it flips a row
        # to `failed` (backend/websocket.py:57). Without explicitly
        # clearing it here, a resurrected row would carry a stale
        # `completed_at` while having `status='running'` — currently
        # harmless because the WS SELECT does not filter the running
        # branch on `completed_at`, but a latent data-integrity bug that
        # any future `AND completed_at IS NULL` defensive guard would
        # silently break.
        result = self._conn.execute(
            """UPDATE progress_tracking
               SET status = 'running',
                   updated_at = now(),
                   completed_at = NULL,
                   progress_data = progress_data || %s::jsonb
               WHERE task_id = %s""",
            (json.dumps(progress_overlay), task_id),
        )
        return result.rowcount

