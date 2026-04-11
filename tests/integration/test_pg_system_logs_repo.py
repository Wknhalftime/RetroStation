"""Integration tests for PgSystemLogRepository."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.domain.enums import LogCategory, LogLevel
from backend.domain.system import SystemLog


def _make_log(
    category: LogCategory = LogCategory.SCAN,
    level: LogLevel = LogLevel.INFO,
    message: str = "test",
    trace_id: str | None = None,
    offset_seconds: int = 0,
    details: dict | None = None,
) -> SystemLog:
    return SystemLog(
        category=category,
        level=level,
        message=message,
        trace_id=trace_id,
        details=details,
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=offset_seconds),
    )


class TestCreateAndList:
    def test_create_and_list_returns_entry(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSystemLogRepository(conn)
            log = _make_log(message="hello")
            repo.create(log)
            conn.commit()
            results = repo.list()
        assert len(results) == 1
        assert results[0].message == "hello"
        assert results[0].category == LogCategory.SCAN
        assert results[0].level == LogLevel.INFO

    def test_list_ordered_descending(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSystemLogRepository(conn)
            repo.create(_make_log(message="first",  offset_seconds=0))
            repo.create(_make_log(message="second", offset_seconds=10))
            repo.create(_make_log(message="third",  offset_seconds=20))
            conn.commit()
            results = repo.list()
        assert [r.message for r in results] == ["third", "second", "first"]

    def test_jsonb_roundtrip(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSystemLogRepository(conn)
            details = {"files": 42, "path": "/music", "nested": {"ok": True}}
            repo.create(_make_log(details=details))
            conn.commit()
            result = repo.list()[0]
        assert result.details == details

    def test_null_details_and_trace_id(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSystemLogRepository(conn)
            repo.create(_make_log(details=None, trace_id=None))
            conn.commit()
            result = repo.list()[0]
        assert result.details is None
        assert result.trace_id is None


class TestFilters:
    def test_filter_by_level(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSystemLogRepository(conn)
            repo.create(_make_log(level=LogLevel.INFO))
            repo.create(_make_log(level=LogLevel.ERROR))
            conn.commit()
            results = repo.list(level=LogLevel.ERROR)
        assert len(results) == 1
        assert results[0].level == LogLevel.ERROR

    def test_filter_by_category(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSystemLogRepository(conn)
            repo.create(_make_log(category=LogCategory.SCAN))
            repo.create(_make_log(category=LogCategory.ENRICHMENT))
            conn.commit()
            results = repo.list(category=LogCategory.ENRICHMENT)
        assert len(results) == 1
        assert results[0].category == LogCategory.ENRICHMENT

    def test_filter_by_trace_id(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSystemLogRepository(conn)
            repo.create(_make_log(trace_id="trace-aaa"))
            repo.create(_make_log(trace_id="trace-bbb"))
            conn.commit()
            results = repo.list(trace_id="trace-aaa")
        assert len(results) == 1
        assert results[0].trace_id == "trace-aaa"

    def test_filter_combined(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSystemLogRepository(conn)
            repo.create(_make_log(category=LogCategory.SCAN, level=LogLevel.ERROR))
            repo.create(_make_log(category=LogCategory.SCAN, level=LogLevel.INFO))
            repo.create(_make_log(category=LogCategory.ENRICHMENT, level=LogLevel.ERROR))
            conn.commit()
            results = repo.list(category=LogCategory.SCAN, level=LogLevel.ERROR)
        assert len(results) == 1


class TestPagination:
    def test_limit(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSystemLogRepository(conn)
            for i in range(10):
                repo.create(_make_log(offset_seconds=i))
            conn.commit()
            results = repo.list(limit=3)
        assert len(results) == 3

    def test_offset(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSystemLogRepository(conn)
            for i in range(6):
                repo.create(_make_log(message=f"msg-{i}", offset_seconds=i))
            conn.commit()
            page0 = repo.list(limit=3, offset=0)
            page1 = repo.list(limit=3, offset=3)
        assert len(page0) == 3
        assert len(page1) == 3
        ids_p0 = {r.id for r in page0}
        ids_p1 = {r.id for r in page1}
        assert ids_p0.isdisjoint(ids_p1)


class TestCount:
    def test_count_all(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSystemLogRepository(conn)
            for _ in range(5):
                repo.create(_make_log())
            conn.commit()
            total = repo.count()
        assert total == 5

    def test_count_filtered(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSystemLogRepository(conn)
            repo.create(_make_log(level=LogLevel.ERROR))
            repo.create(_make_log(level=LogLevel.INFO))
            repo.create(_make_log(level=LogLevel.INFO))
            conn.commit()
            assert repo.count(level=LogLevel.INFO) == 2

    def test_count_empty(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgSystemLogRepository(conn)
            assert repo.count() == 0

