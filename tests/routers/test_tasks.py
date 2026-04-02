from __future__ import annotations

import json
from datetime import datetime, timezone

import psycopg
from fastapi.testclient import TestClient

from backend.db.repositories.progress_tracking import PgProgressTrackingRepository
from backend.domain.enums import TaskStatus, TaskType
from backend.domain.models import ProgressTracking


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str,
    task_type: TaskType = TaskType.SCAN,
    status: TaskStatus = TaskStatus.RUNNING,
    progress_data: dict | None = None,
) -> ProgressTracking:
    now = datetime.now(tz=timezone.utc)
    return ProgressTracking(
        task_id=task_id,
        task_type=task_type,
        status=status,
        progress_data=progress_data or {},
        started_at=now,
        updated_at=now,
        completed_at=None,
    )


def _seed_task(
    conn: psycopg.Connection[dict],
    task_id: str,
    task_type: TaskType = TaskType.SCAN,
    status: TaskStatus = TaskStatus.RUNNING,
    progress_data: dict | None = None,
) -> ProgressTracking:
    repo = PgProgressTrackingRepository(conn)
    task = repo.upsert(_make_task(task_id, task_type, status, progress_data))
    conn.commit()
    return task


# ---------------------------------------------------------------------------
# Tests — GET /api/v1/tasks/active
# ---------------------------------------------------------------------------


class TestActiveTasks:
    def test_empty_returns_empty_list(self, client: TestClient) -> None:
        resp = client.get("/api/v1/tasks/active")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_only_running_tasks(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        _seed_task(db_conn, "task-running-1", status=TaskStatus.RUNNING)
        _seed_task(db_conn, "task-running-2", status=TaskStatus.RUNNING)
        _seed_task(db_conn, "task-completed", status=TaskStatus.COMPLETED)
        _seed_task(db_conn, "task-failed", status=TaskStatus.FAILED)

        resp = client.get("/api/v1/tasks/active")
        assert resp.status_code == 200
        data = resp.json()

        task_ids = {t["task_id"] for t in data}
        assert "task-running-1" in task_ids
        assert "task-running-2" in task_ids
        assert "task-completed" not in task_ids
        assert "task-failed" not in task_ids

    def test_running_task_has_expected_fields(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        _seed_task(
            db_conn,
            "task-scan-1",
            task_type=TaskType.SCAN,
            status=TaskStatus.RUNNING,
            progress_data={"files_scanned": 42, "total": 100},
        )

        resp = client.get("/api/v1/tasks/active")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1

        task = data[0]
        assert task["task_id"] == "task-scan-1"
        assert task["task_type"] == "scan"
        assert task["status"] == "running"
        assert task["progress_data"] == {"files_scanned": 42, "total": 100}
        assert "started_at" in task
        assert "updated_at" in task
        assert task["completed_at"] is None

    def test_ordered_by_started_at_desc(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        """Newer tasks should appear first in the response."""
        import time

        _seed_task(db_conn, "task-older", status=TaskStatus.RUNNING)
        time.sleep(0.05)  # ensure distinct timestamps
        _seed_task(db_conn, "task-newer", status=TaskStatus.RUNNING)

        resp = client.get("/api/v1/tasks/active")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["task_id"] == "task-newer"
        assert data[1]["task_id"] == "task-older"

    def test_no_running_tasks_returns_empty_list(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        _seed_task(db_conn, "task-done", status=TaskStatus.COMPLETED)

        resp = client.get("/api/v1/tasks/active")
        assert resp.status_code == 200
        assert resp.json() == []
