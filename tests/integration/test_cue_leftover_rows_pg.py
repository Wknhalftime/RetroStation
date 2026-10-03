"""A cue run's row left RUNNING, ended on PostgreSQL (PR G2 final review I1; not locked).

Requirements:
- I1: the cue worker's start-up hook ends a ``cue_analysis`` row that a crash or a hard stop
  left RUNNING: FAILED, with a reason, ``completed_at = updated_at``; the ``/ws`` feed's
  query (running rows, plus rows that ended in the last 5 s) then never shows it, and the
  reaper has nothing left to flip;
- no other row is touched: an ended cue row, a running scan, the meter's row.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from backend import websocket
from backend.services.streaming.cue_progress import LEFTOVER_REASON
from backend.tasks import stream_cue_tasks as tasks_module

WS_FEED = """SELECT task_id FROM progress_tracking
   WHERE status = 'running'
      OR (status IN ('completed', 'failed')
          AND coalesce(completed_at, updated_at) > now() - (interval '1 second' * %s))"""
"""The ``/ws`` feed's rows (``backend/websocket.py``): running ones, and ones that ended within
its grace (``TERMINAL_GRACE_SECONDS``)."""

ROWS = [
    ("run-old", "cue_analysis", "running", None),
    ("run-done", "cue_analysis", "completed", "now() - interval '2 hours'"),
    ("scan-1", "scan", "running", None),
    ("stream_resources", "stream_resources", "running", None),
]


def seed(conn: psycopg.Connection[Any]) -> None:
    for task_id, task_type, status, completed in ROWS:
        conn.execute(
            "INSERT INTO progress_tracking (task_id, task_type, status, progress_data, "
            "started_at, updated_at, completed_at) VALUES (%s, %s, %s, %s, "
            "now() - interval '3 hours', now() - interval '1 hour', "
            f"{completed or 'NULL'})",
            (task_id, task_type, status, json.dumps({"processed": 4, "total": 9})),
        )


def snapshot(conn: psycopg.Connection[Any]) -> dict[str, dict[str, Any]]:
    rows = conn.execute("SELECT * FROM progress_tracking ORDER BY task_id").fetchall()
    return {row["task_id"]: row for row in rows}


def test_a_cue_row_left_running_is_failed_at_start_up(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = SimpleNamespace(database_url=migrated_db)
    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)
    with psycopg.connect(migrated_db, autocommit=True, row_factory=dict_row) as conn:
        seed(conn)
        before = snapshot(conn)

        tasks_module.end_leftover_cue_rows()

        after = snapshot(conn)
        ended = after["run-old"]
        assert ended["status"] == "failed"
        assert ended["completed_at"] == ended["updated_at"] == before["run-old"]["updated_at"]
        assert ended["started_at"] == before["run-old"]["started_at"]
        assert ended["progress_data"] == {"processed": 4, "total": 9, "error": LEFTOVER_REASON}
        for task_id in ("run-done", "scan-1", "stream_resources"):
            assert after[task_id] == before[task_id]
        shown = {
            row["task_id"]
            for row in conn.execute(WS_FEED, (websocket.TERMINAL_GRACE_SECONDS,)).fetchall()
        }
        assert shown == {"scan-1", "stream_resources"}
