from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import psycopg
from fastapi.testclient import TestClient

from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str,
    task_type: TaskType = TaskType.SCAN,
    status: TaskStatus = TaskStatus.RUNNING,
    progress_data: dict | None = None,
    started_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> TaskProgress:
    now = datetime.now(tz=UTC)
    return TaskProgress(
        task_id=task_id,
        task_type=task_type,
        status=status,
        progress_data=progress_data or {},
        started_at=started_at or now,
        updated_at=updated_at or now,
        completed_at=None,
    )


def _seed_task(
    conn: psycopg.Connection[dict],
    task_id: str,
    task_type: TaskType = TaskType.SCAN,
    status: TaskStatus = TaskStatus.RUNNING,
    progress_data: dict | None = None,
    started_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> TaskProgress:
    repo = PgTaskProgressRepository(conn)
    task = repo.upsert(
        _make_task(
            task_id, task_type, status, progress_data, started_at, updated_at
        )
    )
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

    def test_excludes_old_terminal_tasks(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        """Terminal tasks updated >30s ago are excluded."""
        from datetime import timedelta

        old = datetime.now(tz=UTC) - timedelta(minutes=5)
        _seed_task(db_conn, "task-running-1", status=TaskStatus.RUNNING)
        _seed_task(db_conn, "task-running-2", status=TaskStatus.RUNNING)
        _seed_task(
            db_conn, "task-completed", status=TaskStatus.COMPLETED,
            updated_at=old,
        )
        _seed_task(
            db_conn, "task-failed", status=TaskStatus.FAILED,
            updated_at=old,
        )

        resp = client.get("/api/v1/tasks/active")
        assert resp.status_code == 200
        data = resp.json()

        task_ids = {t["task_id"] for t in data}
        assert "task-running-1" in task_ids
        assert "task-running-2" in task_ids
        assert "task-completed" not in task_ids
        assert "task-failed" not in task_ids

    def test_returns_recent_failed_and_completed(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        """Terminal tasks updated within 30s appear in the response."""
        _seed_task(
            db_conn, "task-recent-fail", status=TaskStatus.FAILED,
        )
        _seed_task(
            db_conn, "task-recent-done", status=TaskStatus.COMPLETED,
        )

        resp = client.get("/api/v1/tasks/active")
        assert resp.status_code == 200
        data = resp.json()

        task_ids = {t["task_id"] for t in data}
        assert "task-recent-fail" in task_ids
        assert "task-recent-done" in task_ids

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
        from datetime import timedelta

        older = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        newer = older + timedelta(hours=1)

        _seed_task(db_conn, "task-older", status=TaskStatus.RUNNING, started_at=older)
        _seed_task(db_conn, "task-newer", status=TaskStatus.RUNNING, started_at=newer)

        resp = client.get("/api/v1/tasks/active")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["task_id"] == "task-newer"
        assert data[1]["task_id"] == "task-older"

    def test_no_running_tasks_old_completed_returns_empty(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        from datetime import timedelta

        old = datetime.now(tz=UTC) - timedelta(minutes=5)
        _seed_task(
            db_conn, "task-done", status=TaskStatus.COMPLETED,
            updated_at=old,
        )

        resp = client.get("/api/v1/tasks/active")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Tests — POST /api/v1/tasks/retry-enrichment
# ---------------------------------------------------------------------------


class TestRetryEnrichment:
    def test_resets_failed_files_and_returns_count(
        self, client: TestClient
    ) -> None:
        """retry_enrichment endpoint resets failed files and returns count."""
        mock_repo = MagicMock()
        mock_repo.reset_failed_enrichments.return_value = 5

        with (
            patch("backend.routers.tasks.connect_sync") as mock_conn_ctx,
            patch(
                "backend.routers.tasks.PgLibraryFileRepository",
                return_value=mock_repo,
            ),
            patch(
                "backend.tasks.library_enrichment_tasks.library_enrichment_task"
            ),
        ):
            conn_mock = MagicMock()
            mock_conn_ctx.return_value.__enter__ = MagicMock(return_value=conn_mock)
            mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

            resp = client.post("/api/v1/tasks/retry-enrichment")

        assert resp.status_code == 200
        data = resp.json()
        assert data["reset"] == 5
        assert "5" in data["message"]
        mock_repo.reset_failed_enrichments.assert_called_once()
