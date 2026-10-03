"""Progress rows on PostgreSQL (PR G2, Tasks 7 and 8; traceability Y: T7.16, T8.22).

Requirements:
- D94: the telemetry writer's connection sets ``synchronous_commit = off`` itself (the test
  database sets it too, so the setting's source must be the connection: ``client``), with a
  1 s statement and a 0.5 s lock bound;
- D90: one meter row, updated in place, never a row per sample; the ``/ws`` reaper marks a
  row ``failed`` when it was not updated for 10 minutes, and the meter row is written with a
  fresh, UTC-aware ``updated_at`` (I5, through the production clock ``meter_clock``), so the
  reaper never matches it, however long the meter has been running;
- PG13: after an outage longer than 10 minutes, the next write sets the row back to
  ``running`` and clears ``completed_at``;
- D77a: the reaper is the existing one (``websocket.STALE_SQL``, named, unchanged).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from functools import partial
from typing import Any

import psycopg
from psycopg.rows import dict_row

from backend import websocket
from backend.db.progress_writer import ProgressWriter, writer_options
from backend.db.sync_conn import connect_sync
from backend.domain.stream_capacity import CapacityBudget
from backend.main import meter_clock
from backend.playout.process_meter import MachineUsage, ProcessUsage
from backend.services.streaming.resource_meter import meter_row, sample_engines


class OneEngine:
    """A ``ResourceMeter`` with one measured engine on the dev PC."""

    def read(self, pids: Sequence[int]) -> list[ProcessUsage]:
        return [ProcessUsage(pid, 13.0, 75.0) for pid in pids]

    def machine(self) -> MachineUsage:
        return MachineUsage(
            threads=16, memory_total_mb=65_430.0, memory_available_mb=40_100.0, cpu_percent=9.0
        )


def meter_rows(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT status, completed_at FROM progress_tracking WHERE task_type = 'stream_resources'"
    ).fetchall()


def reap(conn: psycopg.Connection[Any]) -> None:
    conn.execute(websocket.STALE_SQL, (websocket.STALE_THRESHOLD_MINUTES,))


def test_a_writer_connection_on_postgres_reports_synchronous_commit_off(migrated_db: str) -> None:
    # T7.16 (D94).
    with connect_sync(
        migrated_db, autocommit=True, connect_timeout=2, options=writer_options()
    ) as conn:
        row = conn.execute(
            "SELECT setting, source FROM pg_settings WHERE name = 'synchronous_commit'"
        ).fetchone()
        assert row is not None
        assert (row["setting"], row["source"]) == ("off", "client")
        timeout = conn.execute("SHOW statement_timeout").fetchone()
        lock = conn.execute("SHOW lock_timeout").fetchone()
    assert timeout is not None and timeout["statement_timeout"] == "1s"
    assert lock is not None and lock["lock_timeout"] == "500ms"


def test_the_reaper_never_matches_a_fresh_meter_row_and_a_flipped_one_comes_back(
    migrated_db: str,
) -> None:
    # T8.22 (D90, I5, PG13): a meter running for 30 minutes, written now, is fresh.
    writer = ProgressWriter(
        partial(
            connect_sync, migrated_db, autocommit=True, connect_timeout=2, options=writer_options()
        )
    )
    started = meter_clock() - timedelta(minutes=30)
    sample = sample_engines([101], OneEngine(), CapacityBudget())
    try:
        with psycopg.connect(migrated_db, autocommit=True, row_factory=dict_row) as conn:
            writer.write(meter_row(sample, started, meter_clock()))
            reap(conn)
            assert [r["status"] for r in meter_rows(conn)] == ["running"]

            writer.write(meter_row(sample, started, meter_clock()))
            reap(conn)
            assert [r["status"] for r in meter_rows(conn)] == ["running"]

            conn.execute(
                "UPDATE progress_tracking SET updated_at = now() - interval '11 minutes' "
                "WHERE task_type = 'stream_resources'"
            )
            reap(conn)
            [flipped] = meter_rows(conn)
            assert flipped["status"] == "failed" and flipped["completed_at"] is not None

            writer.write(meter_row(sample, started, meter_clock()))
            assert meter_rows(conn) == [{"status": "running", "completed_at": None}]
    finally:
        writer.close()
