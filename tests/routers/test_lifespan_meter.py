"""The app's lifespan and the cost meter's row (PR G2, Task 8; traceability T: T8.28, T8.29).

Requirements:
- PG13 and M10: at start-up, before the app serves, a ``stream_resources`` row a crash left
  RUNNING is completed, so the ``/ws`` reaper never flips it to failed; this runs whether or
  not streaming is on (the lifespan client forces it off), through the real PostgreSQL path;
- C1: the lifespan, not ``start_streaming``, wires the meter: it opens the meter's telemetry
  connection with ``meter_connect`` (D94) on the app's own database, hands it to
  ``_prepare_meter``, and at shutdown hands the meter to ``_shutdown``, which stops it before
  streaming (audit MF2).

Both tests run the lifespan on the test's own database only (``tests/lifespan_client.py``);
the locked start-up tests are untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.lifespan_client import lifespan_client


@pytest.fixture
def app_db(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The app configured on the test's database, its migrations already applied."""
    from backend.config import get_settings

    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("RETROSTATION_SKIP_BOOT_MIGRATIONS", "1")
    get_settings.cache_clear()
    yield migrated_db
    get_settings.cache_clear()


def test_startup_ends_a_meter_row_left_running(app_db: str) -> None:
    # T8.28 (PG13, M10; audit MF2): a crash left the meter's row RUNNING an hour ago.
    from backend.main import app

    hour_ago = datetime.now(UTC) - timedelta(hours=1)
    with psycopg.connect(app_db, row_factory=dict_row) as conn:
        conn.execute(
            """INSERT INTO progress_tracking
               (task_id, task_type, status, progress_data, started_at, updated_at)
               VALUES ('stream_resources', 'stream_resources', 'running',
                       '{"open_streams": 2}', %s, %s)""",
            (hour_ago, hour_ago),
        )
        conn.commit()

    with lifespan_client(app), psycopg.connect(app_db, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT status, completed_at FROM progress_tracking WHERE task_id = 'stream_resources'"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["completed_at"] is not None and rows[0]["completed_at"] > hour_ago


def test_the_lifespan_uses_the_telemetry_connection_and_stops_the_meter_first(
    app_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T8.29 (C1, D94, PG13; audit MF2).
    import backend.main as main_module
    from backend.config import get_settings

    connect_sentinel, meter_sentinel = object(), object()
    connected: list[str] = []
    prepared: list[tuple[object, object]] = []
    shut: list[tuple[object, object]] = []

    def meter_connect(url: str) -> object:
        connected.append(url)
        return connect_sentinel

    async def prepare_meter(settings: object, connect: object) -> tuple[None, object]:
        prepared.append((settings, connect))
        return None, meter_sentinel

    async def shutdown(meter: object, streaming: object) -> None:
        shut.append((meter, streaming))

    monkeypatch.setattr(main_module, "meter_connect", meter_connect)
    monkeypatch.setattr(main_module, "_prepare_meter", prepare_meter)
    monkeypatch.setattr(main_module, "_shutdown", shutdown)
    with lifespan_client(main_module.app):
        assert connected == [app_db]
        assert [connect for _, connect in prepared] == [connect_sentinel]
        assert prepared[0][0] is get_settings()
        assert shut == []
    assert shut == [(meter_sentinel, None)]
