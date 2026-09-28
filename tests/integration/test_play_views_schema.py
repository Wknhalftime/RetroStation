"""Acceptance tests: PR C's schema around the play views (spec D17, D19, Data).

The ``stream_cues`` table and the views ``play_file_resolution`` and ``station_day_plays``
ship in one migration, ``00NN_stream_cues.sql``, so its rollback undoes all three. The
station-day filter ``play_date`` is ``(played_at AT TIME ZONE 'UTC')::date``, so
``play_events`` carries an index on that expression.

No migration number appears here: the rollback is found by name, the index by expression.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

import backend.db
from tests.integration.stream_seed import Conn

PLAY_DATE = "((played_at AT TIME ZONE 'UTC')::date)"
"""The ``play_date`` expression of ``station_day_plays`` (D3, D19), as index source text."""


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def rollback_script() -> Path:
    [script] = sorted(Path(backend.db.__file__).parent.glob("rollback_*_stream_cues.sql"))
    return script


def _index_expressions(conn: Conn, table: str) -> list[str | None]:
    """Expressions of the table's single-column, non-partial indexes."""
    rows = conn.execute(
        """SELECT pg_get_expr(i.indexprs, i.indrelid) AS expr
           FROM pg_index i
           WHERE i.indrelid = %s::regclass AND i.indpred IS NULL AND i.indnatts = 1""",
        (table,),
    ).fetchall()
    return [r["expr"] for r in rows]


def _play_date_reference(conn: Conn) -> str:
    """The ``play_date`` index expression as PostgreSQL normalises it."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS play_date_probe (played_at timestamptz)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS play_date_probe_idx ON play_date_probe ({PLAY_DATE})")
    [reference] = _index_expressions(conn, "play_date_probe")
    assert reference is not None
    return reference


def test_rollback_script_drops_the_views(conn: Conn) -> None:
    reference = _play_date_reference(conn)

    def state() -> tuple[bool, bool, bool, bool, int]:
        row = conn.execute(
            r"""SELECT to_regclass('public.stream_cues') IS NOT NULL AS cues,
                       to_regclass('public.play_file_resolution') IS NOT NULL AS resolution,
                       to_regclass('public.station_day_plays') IS NOT NULL AS day_plays,
                       (SELECT count(*) FROM schema_migrations
                        WHERE version LIKE '%\_stream\_cues') AS recorded"""
        ).fetchone()
        assert row is not None
        indexed = reference in _index_expressions(conn, "play_events")
        return row["cues"], row["resolution"], row["day_plays"], indexed, row["recorded"]

    conn.commit()
    assert state() == (True, True, True, True, 1)
    with conn.transaction(force_rollback=True):
        conn.execute(rollback_script().read_text(encoding="utf-8"))
        assert state() == (False, False, False, False, 0)
    assert state() == (True, True, True, True, 1)


def test_play_date_expression_index_exists(conn: Conn) -> None:
    """Matched on the expression as PostgreSQL normalises it, never on the index name."""
    reference = _play_date_reference(conn)

    assert reference in _index_expressions(conn, "play_events")
