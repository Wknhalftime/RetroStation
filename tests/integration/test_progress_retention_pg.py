"""progress_tracking stays small (migration 0039 and the daily prune).

Requirements:
- 0039: no index holds ``updated_at`` (a heartbeat can be a HOT update), the table's pages
  are half full, and the indexes other readers use (primary key, type + status) stay;
- prune: finished rows (completed, failed, timeout) that ended before the cutoff go, running
  rows stay at any age, and the newest completed row of each task type stays at any age, so
  the re-check watermark (``last_completed_started_at``) never moves.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.enums import TaskType
from backend.tasks.progress_prune_tasks import PROGRESS_RETENTION, prune_finished_progress

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
OLD = NOW - PROGRESS_RETENTION - timedelta(days=1)
RECENT = NOW - PROGRESS_RETENTION + timedelta(days=1)


def _row(
    conn: psycopg.Connection[Any],
    task_id: str,
    task_type: TaskType,
    status: str,
    ended: datetime,
    *,
    completed_at: datetime | None = None,
) -> None:
    conn.execute(
        """INSERT INTO progress_tracking
               (task_id, task_type, status, progress_data, started_at, updated_at, completed_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            task_id,
            task_type.value,
            status,
            json.dumps({}),
            ended - timedelta(minutes=5),
            ended,
            completed_at,
        ),
    )


def _ids(conn: psycopg.Connection[Any]) -> set[str]:
    return {r["task_id"] for r in conn.execute("SELECT task_id FROM progress_tracking")}


def test_0039_leaves_no_index_on_updated_at(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'progress_tracking'"
        ).fetchall()

    names = {r["indexname"] for r in rows}
    assert "updated_at" not in " ".join(r["indexdef"] for r in rows)
    assert {"progress_tracking_pkey", "idx_progress_type_status"} <= names


def test_0039_sets_half_full_pages(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        row = conn.execute(
            "SELECT reloptions FROM pg_class WHERE relname = 'progress_tracking'"
        ).fetchone()

    assert row is not None
    assert "fillfactor=50" in (row[0] or [])


def test_prune_deletes_old_finished_rows_and_keeps_the_rest(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        scan = TaskType.SCAN
        _row(conn, "scan-old-done", scan, "completed", OLD, completed_at=OLD)
        _row(conn, "scan-old-failed", scan, "failed", OLD, completed_at=OLD)
        _row(conn, "scan-old-timeout", scan, "timeout", OLD)
        _row(conn, "scan-old-running", scan, "running", OLD)
        _row(conn, "scan-recent-done", scan, "completed", RECENT, completed_at=RECENT)
        # completed_at, not updated_at, decides when a row ended.
        _row(conn, "scan-recent-end", scan, "failed", OLD, completed_at=RECENT)
        conn.commit()

        deleted = prune_finished_progress(PgTaskProgressRepository(conn), NOW)
        conn.commit()

        assert deleted == 3
        assert _ids(conn) == {"scan-old-running", "scan-recent-done", "scan-recent-end"}


def test_prune_keeps_each_types_newest_completed_row(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        recheck = TaskType.MATCHING_RECHECK
        _row(conn, "recheck-older", recheck, "completed", OLD - timedelta(days=3), completed_at=OLD)
        _row(conn, "recheck-newest", recheck, "completed", OLD, completed_at=OLD)
        _row(conn, "recheck-failed-after", recheck, "failed", OLD, completed_at=OLD)
        _row(conn, "scan-only", TaskType.SCAN, "completed", OLD, completed_at=OLD)
        conn.commit()
        repo = PgTaskProgressRepository(conn)
        watermark = repo.last_completed_started_at(recheck)

        prune_finished_progress(repo, NOW)
        conn.commit()

        assert _ids(conn) == {"recheck-newest", "scan-only"}
        assert repo.last_completed_started_at(recheck) == watermark
