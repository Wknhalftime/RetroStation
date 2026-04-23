"""Integration tests for the shared task-failure telemetry helper.

These tests hit a real Postgres via the `migrated_db` fixture — the helper's
contract is that when a wrapped task raises, a FAILED TaskProgress row and an
ERROR SystemLog entry land in the DB, and the original exception is
re-raised. The simplest honest way to verify both guarantees is to run
against the actual repo implementations.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from backend.domain.enums import LogCategory, LogLevel, TaskStatus, TaskType
from backend.tasks._error_boundary import task_failure_telemetry

pytestmark = pytest.mark.integration


@pytest.fixture
def settings_db_url(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """Point `get_settings()` at the migrated test DB for the duration of
    one test. Clears the lru_cache both on entry AND on teardown — the
    second clear prevents a leaked Settings instance (still holding the
    test DB URL) from polluting later tests once monkeypatch restores
    the env var.
    """
    from backend.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    get_settings.cache_clear()
    try:
        yield migrated_db
    finally:
        # After monkeypatch rolls back DATABASE_URL, force the next call
        # to get_settings() to rebuild from the restored environment.
        get_settings.cache_clear()


def test_task_failure_telemetry_writes_failed_row_and_reraises(
    migrated_db: str, settings_db_url: str
) -> None:

    with (
        pytest.raises(RuntimeError, match="deliberate"),
        task_failure_telemetry(TaskType.MATCHING, LogCategory.MATCHING) as task_id,
    ):
        raise RuntimeError("deliberate")

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        progress_row = conn.execute(
            "SELECT status, progress_data FROM progress_tracking WHERE task_id = %s",
            (task_id,),
        ).fetchone()
        assert progress_row is not None
        assert progress_row["status"] == TaskStatus.FAILED.value
        assert progress_row["progress_data"]["error"] == "deliberate"

        log_row = conn.execute(
            "SELECT level, message, details FROM system_logs WHERE trace_id = %s",
            (task_id,),
        ).fetchone()
        assert log_row is not None
        assert log_row["level"] == LogLevel.ERROR.value
        assert log_row["message"] == f"{TaskType.MATCHING.value}_failed"
        assert log_row["details"]["error"] == "deliberate"


def test_task_failure_telemetry_no_writes_on_success(
    migrated_db: str, settings_db_url: str
) -> None:
    with task_failure_telemetry(TaskType.MATCHING, LogCategory.MATCHING) as task_id:
        pass  # no exception

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        progress_row = conn.execute(
            "SELECT 1 FROM progress_tracking WHERE task_id = %s", (task_id,),
        ).fetchone()
        assert progress_row is None
        log_row = conn.execute(
            "SELECT 1 FROM system_logs WHERE trace_id = %s", (task_id,),
        ).fetchone()
        assert log_row is None


def test_task_failure_telemetry_caller_supplied_task_id(
    migrated_db: str, settings_db_url: str
) -> None:
    supplied = "my-explicit-trace-id"

    with (
        pytest.raises(ValueError, match="boom"),
        task_failure_telemetry(
            TaskType.MATCHING, LogCategory.MATCHING, task_id=supplied,
        ) as task_id,
    ):
        assert task_id == supplied
        raise ValueError("boom")

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT status FROM progress_tracking WHERE task_id = %s", (supplied,),
        ).fetchone()
        assert row is not None
        assert row["status"] == TaskStatus.FAILED.value


def test_task_failure_telemetry_catches_uuid_parse_errors(
    migrated_db: str, settings_db_url: str
) -> None:
    """Regression: UUID parsing INSIDE the boundary must be caught. Review
    PR#21 noted that evaluating UUID(playlist_id) before entering the
    context bypassed the FAILED row — this test pins the fix.
    """
    supplied = "uuid-regression-trace"

    with (
        pytest.raises(ValueError),
        task_failure_telemetry(
            TaskType.LIBRARY_ENRICHMENT,
            LogCategory.ENRICHMENT,
            task_id=supplied,
        ),
    ):
        from uuid import UUID
        UUID("not-a-valid-uuid")  # raises ValueError

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT status FROM progress_tracking WHERE task_id = %s", (supplied,),
        ).fetchone()
        assert row is not None
        assert row["status"] == TaskStatus.FAILED.value
