"""Router tests for GET /api/v1/system-logs and /api/v1/system-logs/by-trace/{trace_id}."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
from fastapi.testclient import TestClient

from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.domain.enums import LogCategory, LogLevel
from backend.domain.models import SystemLog

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _seed_log(
    conn: psycopg.Connection[dict],
    *,
    category: LogCategory = LogCategory.SCAN,
    level: LogLevel = LogLevel.INFO,
    message: str = "test",
    trace_id: str | None = None,
    details: dict | None = None,
    offset_seconds: int = 0,
) -> None:
    repo = PgSystemLogRepository(conn)
    repo.create(SystemLog(
        category=category,
        level=level,
        message=message,
        trace_id=trace_id,
        details=details,
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=offset_seconds),
    ))
    conn.commit()


# ---------------------------------------------------------------------------
# GET /api/v1/system-logs
# ---------------------------------------------------------------------------


class TestListSystemLogs:
    def test_empty_returns_zero_total(self, client: TestClient) -> None:
        resp = client.get("/api/v1/system-logs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_returns_seeded_entries(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        _seed_log(db_conn, message="scan_started")
        _seed_log(db_conn, message="scan_completed", offset_seconds=5)
        resp = client.get("/api/v1/system-logs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    def test_filter_by_level(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        _seed_log(db_conn, level=LogLevel.INFO)
        _seed_log(db_conn, level=LogLevel.ERROR)
        resp = client.get("/api/v1/system-logs?level=ERROR")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["level"] == "ERROR"

    def test_filter_by_category(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        _seed_log(db_conn, category=LogCategory.SCAN)
        _seed_log(db_conn, category=LogCategory.ENRICHMENT)
        resp = client.get("/api/v1/system-logs?category=enrichment")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["category"] == "enrichment"

    def test_filter_by_trace_id(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        _seed_log(db_conn, trace_id="task-aaa")
        _seed_log(db_conn, trace_id="task-bbb")
        resp = client.get("/api/v1/system-logs?trace_id=task-aaa")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["trace_id"] == "task-aaa"

    def test_pagination(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        for i in range(5):
            _seed_log(db_conn, offset_seconds=i)
        resp = client.get("/api/v1/system-logs?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_details_included_in_response(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        _seed_log(db_conn, details={"files": 7, "root": "/music"})
        resp = client.get("/api/v1/system-logs")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["details"] == {"files": 7, "root": "/music"}


# ---------------------------------------------------------------------------
# GET /api/v1/system-logs/by-trace/{trace_id}
# ---------------------------------------------------------------------------


class TestGetByTrace:
    def test_returns_entries_for_trace(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        _seed_log(db_conn, trace_id="t1", message="start", offset_seconds=0)
        _seed_log(db_conn, trace_id="t1", message="done",  offset_seconds=5)
        _seed_log(db_conn, trace_id="t2", message="other")
        resp = client.get("/api/v1/system-logs/by-trace/t1")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        assert all(i["trace_id"] == "t1" for i in items)

    def test_ordered_ascending(
        self, client: TestClient, db_conn: psycopg.Connection[dict]
    ) -> None:
        _seed_log(db_conn, trace_id="tx", message="first",  offset_seconds=0)
        _seed_log(db_conn, trace_id="tx", message="second", offset_seconds=10)
        resp = client.get("/api/v1/system-logs/by-trace/tx")
        items = resp.json()
        assert items[0]["message"] == "first"
        assert items[1]["message"] == "second"

    def test_unknown_trace_returns_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v1/system-logs/by-trace/nonexistent")
        assert resp.status_code == 200
        assert resp.json() == []

