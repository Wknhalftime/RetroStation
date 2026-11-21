"""D38's holding table ``work_artist_seed`` in tests (work merges phase 3b PR 5b, SPEC D38, D49).

Migration 0055 creates the seed: each work's artist as it stood before ``works.artist_id``
retired. D1's step drops it in the transaction that sets its marker (D49), the one run-once step
that changes the schema. The integration tests share one migrated database per worker, cleaned
by TRUNCATE, so a test that lets D1's step finish puts the table back afterwards, from the
migration's own ``CREATE TABLE``, as S5's index test drops the index its task built.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import tuple_row

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "backend" / "db" / "migrations" / "0055_work_artists_from_credits.sql"
ROLLBACK = ROOT / "backend" / "db" / "rollback_0055_work_artists_from_credits.sql"
TABLE = "work_artist_seed"


def create_statement() -> str:
    """The migration's ``CREATE TABLE work_artist_seed (...);``, made idempotent."""
    text = MIGRATION.read_text(encoding="utf-8")
    found = re.search(rf"CREATE TABLE {TABLE} \(.*?\);", text, re.S)
    assert found is not None, f"{MIGRATION.name} creates no {TABLE}"
    return found.group(0).replace(f"CREATE TABLE {TABLE}", f"CREATE TABLE IF NOT EXISTS {TABLE}")


def exists(conn: psycopg.Connection[Any]) -> bool:
    cur = conn.cursor(row_factory=tuple_row)
    row = cur.execute("SELECT to_regclass(%s) IS NOT NULL", (TABLE,)).fetchone()
    return bool(row and row[0])


def restore(dsn: str) -> None:
    """Put the seed table back, empty, if a test's D1 step dropped it."""
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(create_statement())


def seed(conn: psycopg.Connection[Any], work_id: str, artist_id: str) -> None:
    """The migration's copy of one work's artist."""
    conn.execute(
        f"INSERT INTO {TABLE} (work_id, artist_id) VALUES (%s, %s)",  # noqa: S608
        (work_id, artist_id),
    )


@pytest.fixture
def seed_table_kept(migrated_db: str) -> Iterator[None]:
    """The seed table exists before the test and again after it, whatever D1's step did."""
    restore(migrated_db)
    yield
    restore(migrated_db)
