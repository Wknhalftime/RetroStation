"""Unit tests for FakeSystemLogRepository."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend.domain.enums import LogCategory, LogLevel
from backend.domain.system import SystemLog
from tests.fakes.system_logs import FakeSystemLogRepository


def _make_log(
    category: LogCategory = LogCategory.SCAN,
    level: LogLevel = LogLevel.INFO,
    message: str = "test",
    trace_id: str | None = None,
    offset_seconds: int = 0,
) -> SystemLog:
    return SystemLog(
        category=category,
        level=level,
        message=message,
        trace_id=trace_id,
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=offset_seconds),
    )


# ---------------------------------------------------------------------------
# create / list
# ---------------------------------------------------------------------------

def test_create_and_list_returns_entry() -> None:
    repo = FakeSystemLogRepository()
    log = _make_log()
    repo.create(log)
    results = repo.list()
    assert len(results) == 1
    assert results[0].id == log.id


def test_list_ordered_by_created_at_descending() -> None:
    repo = FakeSystemLogRepository()
    repo.create(_make_log(message="first",  offset_seconds=0))
    repo.create(_make_log(message="second", offset_seconds=10))
    repo.create(_make_log(message="third",  offset_seconds=20))
    results = repo.list()
    assert [r.message for r in results] == ["third", "second", "first"]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_filter_by_level() -> None:
    repo = FakeSystemLogRepository()
    repo.create(_make_log(level=LogLevel.INFO))
    repo.create(_make_log(level=LogLevel.ERROR))
    assert len(repo.list(level=LogLevel.ERROR)) == 1
    assert repo.list(level=LogLevel.ERROR)[0].level == LogLevel.ERROR


def test_filter_by_category() -> None:
    repo = FakeSystemLogRepository()
    repo.create(_make_log(category=LogCategory.SCAN))
    repo.create(_make_log(category=LogCategory.ENRICHMENT))
    results = repo.list(category=LogCategory.SCAN)
    assert len(results) == 1
    assert results[0].category == LogCategory.SCAN


def test_filter_by_trace_id() -> None:
    repo = FakeSystemLogRepository()
    repo.create(_make_log(trace_id="abc-123"))
    repo.create(_make_log(trace_id="xyz-999"))
    results = repo.list(trace_id="abc-123")
    assert len(results) == 1
    assert results[0].trace_id == "abc-123"


def test_filter_combined() -> None:
    repo = FakeSystemLogRepository()
    repo.create(_make_log(category=LogCategory.SCAN, level=LogLevel.ERROR, trace_id="t1"))
    repo.create(_make_log(category=LogCategory.SCAN, level=LogLevel.INFO,  trace_id="t1"))
    repo.create(_make_log(category=LogCategory.ENRICHMENT, level=LogLevel.ERROR, trace_id="t1"))
    results = repo.list(category=LogCategory.SCAN, level=LogLevel.ERROR, trace_id="t1")
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def test_pagination_limit() -> None:
    repo = FakeSystemLogRepository()
    for i in range(10):
        repo.create(_make_log(offset_seconds=i))
    assert len(repo.list(limit=3)) == 3


def test_pagination_offset() -> None:
    repo = FakeSystemLogRepository()
    for i in range(5):
        repo.create(_make_log(message=f"msg-{i}", offset_seconds=i))
    page0 = repo.list(limit=2, offset=0)
    page1 = repo.list(limit=2, offset=2)
    assert len(page0) == 2
    assert len(page1) == 2
    assert page0[0].id != page1[0].id


# ---------------------------------------------------------------------------
# count
# ---------------------------------------------------------------------------

def test_count_all() -> None:
    repo = FakeSystemLogRepository()
    for _ in range(7):
        repo.create(_make_log())
    assert repo.count() == 7


def test_count_filtered() -> None:
    repo = FakeSystemLogRepository()
    repo.create(_make_log(level=LogLevel.ERROR))
    repo.create(_make_log(level=LogLevel.INFO))
    repo.create(_make_log(level=LogLevel.INFO))
    assert repo.count(level=LogLevel.INFO) == 2


def test_count_empty() -> None:
    repo = FakeSystemLogRepository()
    assert repo.count() == 0

