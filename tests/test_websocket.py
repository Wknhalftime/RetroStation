"""Tests for the WebSocket progress broadcast endpoint (/ws)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Generator
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress

# psycopg async requires SelectorEventLoop on Windows (not ProactorEventLoop)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _ws_client(_migrated_db_url: str) -> Generator[TestClient]:
    """Session-scoped TestClient with the real token in settings.

    We do NOT override get_current_token here because the WebSocket uses its
    own token check (query param), not the FastAPI dependency.
    """
    os.environ["DATABASE_URL"] = _migrated_db_url

    from backend.config import get_settings

    get_settings.cache_clear()

    from backend.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    get_settings.cache_clear()


@pytest.fixture()
def ws_client(_ws_client: TestClient, migrated_db: str) -> TestClient:
    """Per-test client: tables are already truncated by migrated_db."""
    return _ws_client


@pytest.fixture()
def db_conn(migrated_db: str) -> Generator[psycopg.Connection[dict]]:  # type: ignore[type-arg]
    """Sync connection for inserting test data."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_task(
    conn: psycopg.Connection[dict],  # type: ignore[type-arg]
    task_id: str,
    status: TaskStatus = TaskStatus.RUNNING,
    progress_data: dict | None = None,
) -> TaskProgress:
    now = datetime.now(tz=UTC)
    task = TaskProgress(
        task_id=task_id,
        task_type=TaskType.SCAN,
        status=status,
        progress_data=progress_data or {},
        started_at=now,
        updated_at=now,
        completed_at=None,
    )
    conn.execute(
        """INSERT INTO progress_tracking
           (task_id, task_type, status, progress_data, started_at, updated_at, completed_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            task.task_id,
            task.task_type.value,
            task.status.value,
            json.dumps(task.progress_data),
            task.started_at,
            task.updated_at,
            task.completed_at,
        ),
    )
    conn.commit()
    return task


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWebSocketAuth:
    def test_rejects_bad_token(self, ws_client: TestClient) -> None:
        """Connection with a wrong token should be closed with code 4001.

        Starlette's TestClient raises WebSocketDisconnect from __enter__
        when the server closes the connection before accepting it.
        """
        from starlette.websockets import WebSocketDisconnect

        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            ws_client.websocket_connect("/ws?token=wrong-token"),
        ):
            pass  # pragma: no cover

        assert exc_info.value.code == 4001

    def test_rejects_missing_token(self, ws_client: TestClient) -> None:
        """Connection with no token should be closed with code 4001."""
        from starlette.websockets import WebSocketDisconnect

        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            ws_client.websocket_connect("/ws"),
        ):
            pass  # pragma: no cover

        assert exc_info.value.code == 4001


class TestWebSocketBroadcast:
    def test_connects_with_valid_token(self, ws_client: TestClient) -> None:
        """A valid token allows the connection and produces a tasks payload."""
        with ws_client.websocket_connect("/ws?token=dev-token") as ws:
            data = ws.receive_json()

        assert "tasks" in data
        assert isinstance(data["tasks"], list)

    def test_broadcasts_running_tasks(
        self,
        ws_client: TestClient,
        db_conn: psycopg.Connection[dict],  # type: ignore[type-arg]
    ) -> None:
        """A running task seeded before connecting appears in the first broadcast."""
        _seed_task(
            db_conn,
            "ws-task-1",
            status=TaskStatus.RUNNING,
            progress_data={"files_scanned": 7},
        )

        with ws_client.websocket_connect("/ws?token=dev-token") as ws:
            data = ws.receive_json()

        assert "tasks" in data
        task_ids = [t["task_id"] for t in data["tasks"]]
        assert "ws-task-1" in task_ids

        task = next(t for t in data["tasks"] if t["task_id"] == "ws-task-1")
        assert task["task_type"] == "scan"
        assert task["status"] == "running"
        assert task["progress_data"] == {"files_scanned": 7}
        assert "started_at" in task
        assert "updated_at" in task

    def test_completed_tasks_not_broadcast(
        self,
        ws_client: TestClient,
        db_conn: psycopg.Connection[dict],  # type: ignore[type-arg]
    ) -> None:
        """Completed and failed tasks outside the grace window are excluded."""
        _seed_task(db_conn, "ws-done", status=TaskStatus.COMPLETED)
        _seed_task(db_conn, "ws-failed", status=TaskStatus.FAILED)

        db_conn.execute(
            "UPDATE progress_tracking SET updated_at = now() - interval '10 seconds'"
        )
        db_conn.commit()

        with ws_client.websocket_connect("/ws?token=dev-token") as ws:
            data = ws.receive_json()

        task_ids = [t["task_id"] for t in data["tasks"]]
        assert "ws-done" not in task_ids
        assert "ws-failed" not in task_ids

    def test_completed_tasks_grace_window(
        self,
        ws_client: TestClient,
        db_conn: psycopg.Connection[dict],  # type: ignore[type-arg]
    ) -> None:
        """Completed and failed tasks within the 5s grace window are broadcast."""
        _seed_task(db_conn, "ws-done-recent", status=TaskStatus.COMPLETED)
        _seed_task(db_conn, "ws-failed-recent", status=TaskStatus.FAILED)

        with ws_client.websocket_connect("/ws?token=dev-token") as ws:
            data = ws.receive_json()

        task_ids = [t["task_id"] for t in data["tasks"]]
        assert "ws-done-recent" in task_ids
        assert "ws-failed-recent" in task_ids

    def test_grace_window_uses_completed_at_when_present(
        self,
        ws_client: TestClient,
        db_conn: psycopg.Connection[dict],  # type: ignore[type-arg]
    ) -> None:
        """Rows with non-null ``completed_at`` are filtered by that column.

        The broadcast query uses ``coalesce(completed_at, updated_at)``; when
        ``completed_at`` is set, a fresh ``updated_at`` must not keep a stale
        terminal row visible past the grace window.
        """
        _seed_task(db_conn, "ws-done-old", status=TaskStatus.COMPLETED)

        # completed_at is 10s ago (past the 5s grace); updated_at is fresh.
        db_conn.execute(
            "UPDATE progress_tracking "
            "SET completed_at = now() - interval '10 seconds', "
            "    updated_at = now() "
            "WHERE task_id = 'ws-done-old'"
        )
        db_conn.commit()

        with ws_client.websocket_connect("/ws?token=dev-token") as ws:
            data = ws.receive_json()

        task_ids = [t["task_id"] for t in data["tasks"]]
        assert "ws-done-old" not in task_ids

    def test_grace_window_includes_recent_completed_at(
        self,
        ws_client: TestClient,
        db_conn: psycopg.Connection[dict],  # type: ignore[type-arg]
    ) -> None:
        """Rows with recent ``completed_at`` but stale ``updated_at`` broadcast."""
        _seed_task(db_conn, "ws-done-fresh", status=TaskStatus.COMPLETED)

        # completed_at is fresh (within grace); updated_at is well past it.
        db_conn.execute(
            "UPDATE progress_tracking "
            "SET completed_at = now(), "
            "    updated_at = now() - interval '30 seconds' "
            "WHERE task_id = 'ws-done-fresh'"
        )
        db_conn.commit()

        with ws_client.websocket_connect("/ws?token=dev-token") as ws:
            data = ws.receive_json()

        task_ids = [t["task_id"] for t in data["tasks"]]
        assert "ws-done-fresh" in task_ids
        task = next(t for t in data["tasks"] if t["task_id"] == "ws-done-fresh")
        assert task["completed_at"] is not None
