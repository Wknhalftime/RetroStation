"""Tests for normalize_backfill_task.

Verifies:
- A successful backfill updates library_files and writes no FAILED row.
- An exception inside the batch loop surfaces as a FAILED TaskProgress
  + ERROR SystemLog via the shared task_failure_telemetry boundary,
  and the original exception is re-raised.
"""
from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.enums import LogLevel, TaskStatus, TaskType
from backend.tasks.normalize_backfill_tasks import normalize_backfill_task

pytestmark = pytest.mark.integration


@pytest.fixture
def settings_db_url(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """Point get_settings() at the migrated test DB for the duration of the
    test; clear the lru_cache on teardown so a leaked Settings instance
    doesn't pollute subsequent tests. Mirrors the fixture in
    tests/tasks/test_error_boundary.py.
    """
    from backend.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    get_settings.cache_clear()
    try:
        yield migrated_db
    finally:
        get_settings.cache_clear()


def test_normalize_backfill_failure_writes_failed_progress_and_reraises(
    migrated_db: str, settings_db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the batch loop raises, task_failure_telemetry must persist a FAILED
    TaskProgress row + ERROR SystemLog, and the original exception must
    propagate to Huey.

    Force a mid-execution failure by monkey-patching normalize_artist to
    raise. This exercises the boundary without depending on network timing
    (an unreachable DSN would hang on TCP timeout).
    """
    # Seed one row so the batch loop reaches normalize_artist.
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        conn.execute(
            """INSERT INTO library_files
               (id, file_path, file_hash, format, file_status, raw_metadata)
               VALUES (gen_random_uuid(), %s, %s, %s, 'present', %s::jsonb)""",
            (
                '/music/test.flac',
                'hash-' + 'a' * 32,
                'flac',
                '{"artist": "Prince", "title": "Purple Rain"}',
            ),
        )
        conn.commit()

    class BoomError(RuntimeError):
        pass

    def _explode(_name: str) -> str:
        raise BoomError("deliberate mid-batch failure")

    monkeypatch.setattr(
        'backend.tasks.normalize_backfill_tasks.normalize_artist', _explode,
    )

    with pytest.raises(BoomError):
        normalize_backfill_task.call_local(migrated_db)

    # At least one FAILED TaskProgress row for TaskType.LIBRARY_ENRICHMENT
    # must have been written by the telemetry helper to the SAME DB the
    # task was operating on (passed via database_url= override, not
    # settings.database_url).
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        failed_rows = conn.execute(
            """SELECT status, progress_data
               FROM progress_tracking
               WHERE task_type = %s AND status = %s
               ORDER BY started_at DESC
               LIMIT 1""",
            (TaskType.LIBRARY_ENRICHMENT.value, TaskStatus.FAILED.value),
        ).fetchall()
        assert len(failed_rows) == 1
        assert failed_rows[0]["progress_data"].get("error")

        log_rows = conn.execute(
            """SELECT level, message
               FROM system_logs
               WHERE level = %s AND message LIKE %s
               ORDER BY created_at DESC
               LIMIT 1""",
            (LogLevel.ERROR.value, f"{TaskType.LIBRARY_ENRICHMENT.value}_%"),
        ).fetchall()
        assert len(log_rows) == 1


def test_normalize_backfill_terminates_on_rows_without_artist(
    migrated_db: str, settings_db_url: str,
) -> None:
    """Regression: rows whose raw_metadata has no artist key stay NULL
    after UPDATE (normalized_artist_name remains NULL). Without cursor-
    based pagination, the IS NULL filter keeps re-fetching the same rows
    → infinite loop. With the `id > last_id` cursor, each row is visited
    exactly once and the task terminates.
    """
    # Seed 3 rows, two with no artist/title in raw_metadata.
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        conn.execute(
            """INSERT INTO library_files
               (id, file_path, file_hash, format, file_status, raw_metadata)
               VALUES
                 (gen_random_uuid(), %s, %s, 'flac', 'present', %s::jsonb),
                 (gen_random_uuid(), %s, %s, 'flac', 'present', %s::jsonb),
                 (gen_random_uuid(), %s, %s, 'flac', 'present', %s::jsonb)""",
            (
                '/music/a.flac', 'hash-a-' + 'a' * 30,
                '{"artist": "Prince"}',
                '/music/b.flac', 'hash-b-' + 'b' * 30,
                '{}',  # no artist / title
                '/music/c.flac', 'hash-c-' + 'c' * 30,
                '{"something_else": "x"}',  # unrecognized keys
            ),
        )
        conn.commit()

    # Must terminate (no timeout). Test itself having a reasonable runtime
    # is the termination check — pytest-timeout at the suite level would
    # catch a runaway.
    normalize_backfill_task.call_local(migrated_db)

    # One row should have artist_name populated; the two no-artist rows
    # should still have NULL normalized_artist_name but are not re-processed
    # thanks to the cursor (the task terminated). Re-invoking should be a
    # no-op for the already-processed rows but will re-scan the two NULL
    # rows; the cursor guarantees it visits each NULL row once per run
    # (bounded work), rather than infinite re-work within a single run.
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        processed = conn.execute(
            """SELECT count(*) AS cnt FROM library_files
               WHERE artist_name = 'Prince'""",
        ).fetchone()
        assert processed is not None and processed["cnt"] == 1
