"""The cue worker ends the run rows a crash left RUNNING (PR G2 final review I1; not locked).

Requirements:
- I1: at the cue worker's start (``-w 1``: no run is live yet) a cue run's row left RUNNING
  is ended; the real database round trip is in
  ``tests/integration/test_cue_leftover_rows_pg.py``;
- error handling at its own layer: a database that cannot be reached is translated by the db
  layer (``StorageUnavailableError``) and logged once; the worker still starts (the hook
  never raises).
"""

from __future__ import annotations

from types import SimpleNamespace

import psycopg
import pytest
from structlog.testing import capture_logs

from backend.tasks import stream_cue_tasks as tasks_module
from backend.tasks.cue_huey_app import cue_huey


def test_the_hook_is_registered_on_the_cue_consumer() -> None:
    assert cue_huey._startup.get("end_leftover_cue_rows") is tasks_module.end_leftover_cue_rows


def test_a_database_that_cannot_be_reached_is_logged_and_the_worker_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def refused(*args: object, **kwargs: object) -> object:
        calls.append(kwargs)
        raise psycopg.OperationalError("connection refused")

    url = "postgresql://unused"
    monkeypatch.setattr(tasks_module, "get_settings", lambda: SimpleNamespace(database_url=url))
    monkeypatch.setattr(tasks_module, "connect_sync", refused)
    with capture_logs() as logs:
        tasks_module.end_leftover_cue_rows()
    assert [(e["event"], e["log_level"]) for e in logs] == [
        ("cue_progress_leftover_unended", "warning")
    ]
    # The telemetry connection's bounds (D94): it never stalls the worker's start.
    assert calls == [
        {"autocommit": True, "connect_timeout": 2, "options": tasks_module.writer_options()}
    ]
