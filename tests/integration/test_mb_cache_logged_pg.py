"""``mb_cache`` becomes a logged table (MusicBrainz cache fill, phase 4.1 PR 1; SPEC S1; design
C42; AUD-R042, AUD-R090).

0020 made the cache UNLOGGED, so a crash empties it. Under C29 the cache is the source of truth
(matching reads only the cache from 4.2 on), so 0057 makes it LOGGED again. SET LOGGED keeps the
rows: it rewrites the table into the WAL under an ACCESS EXCLUSIVE lock. The migration sets
``lock_timeout`` first (C45, the migration lint), so a reader holding the table fails the deploy
loudly instead of making it wait.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0057_mb_cache_logged"
MIGRATION = ROOT / "backend" / "db" / "migrations" / f"{VERSION}.sql"

pytestmark = pytest.mark.integration


def _statements(sql: str) -> list[str]:
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return [" ".join(s.split()) for s in without_comments.split(";") if s.strip()]


def _persistence(conn: psycopg.Connection[Any], relation: str) -> str:
    row = conn.execute(
        "SELECT relpersistence FROM pg_class WHERE oid = %s::regclass", (relation,)
    ).fetchone()
    assert row is not None
    return str(row["relpersistence"])


def _put(conn: psycopg.Connection[Any], key: str) -> None:
    conn.execute(
        """INSERT INTO mb_cache (cache_key, entity_type, entity_mbid, response_data, expires_at)
           VALUES (%s, 'artist', '', '{"id": "kept"}', now() + interval '1 day')""",
        (key,),
    )


def test_the_cache_and_its_indexes_are_logged(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        indexes = [
            r["indexname"]
            for r in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'mb_cache'"
            ).fetchall()
        ]

        assert _persistence(conn, "mb_cache") == "p"
        assert indexes, "mb_cache has its indexes"
        assert {i: _persistence(conn, i) for i in indexes} == {i: "p" for i in indexes}


def test_the_migration_is_recorded(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT 1 AS found FROM schema_migrations WHERE version = %s", (VERSION,)
        ).fetchone()

        assert row is not None


def test_the_lock_timeout_comes_first_and_nothing_else_but_the_switch() -> None:
    """C45: ``SET LOCAL lock_timeout`` before anything that locks; then only the switch, so the
    migration copies no rows (it needs no ANALYZE) and touches no other table."""
    statements = _statements(MIGRATION.read_text(encoding="utf-8"))

    assert statements[0].startswith("SET LOCAL lock_timeout")
    assert [s.upper() for s in statements[1:]] == ["ALTER TABLE MB_CACHE SET LOGGED"]


def test_the_switch_keeps_the_rows(migrated_db: str) -> None:
    """From UNLOGGED, as 0020 left it, with a row in it: 0057 makes the table LOGGED and the row
    is still there. In one transaction, rolled back, so the shared test database is untouched."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        conn.execute("ALTER TABLE mb_cache SET UNLOGGED")
        _put(conn, "artist:kept-through-the-switch")
        assert _persistence(conn, "mb_cache") == "u"

        conn.execute(MIGRATION.read_text(encoding="utf-8"))

        assert _persistence(conn, "mb_cache") == "p"
        row = conn.execute(
            "SELECT response_data FROM mb_cache WHERE cache_key = %s",
            ("artist:kept-through-the-switch",),
        ).fetchone()
        assert row is not None and row["response_data"] == {"id": "kept"}
        conn.rollback()


@pytest.mark.slow
def test_a_held_lock_fails_the_migration(migrated_db: str) -> None:
    """While another transaction reads the cache (ACCESS SHARE), the migration gives up after its
    own ``lock_timeout`` with an error instead of waiting. The test session's own lock_timeout is
    turned off first, so only the migration's counts; without it the wait would end at the
    statement timeout instead (QueryCanceled, not LockNotAvailable)."""
    with psycopg.connect(migrated_db) as reader:
        reader.execute("SELECT count(*) FROM mb_cache").fetchone()  # holds ACCESS SHARE
        with psycopg.connect(migrated_db) as conn:
            conn.execute("SET LOCAL lock_timeout = 0")
            conn.execute("SET LOCAL statement_timeout = '20s'")

            with pytest.raises(psycopg.errors.LockNotAvailable):
                conn.execute(MIGRATION.read_text(encoding="utf-8"))
            conn.rollback()
        reader.rollback()
