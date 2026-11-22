"""0058, the cache fill's confirmations queue (MusicBrainz cache fill, phase 4.1 PR 2; SPEC S3; M7;
R3; AUD-R091), on PostgreSQL.

``mb_cache_fill_confirmations`` holds (log name, title) pairs a later step queues for the fill to
search, such as 4.4's add-only pass: one row per name and normalized title, with the title as the
log wrote it and when it was queued. Its key to ``broadcast_artists`` cascades, so deleting a log
name deletes its queued pairs (R3). The fill leaves the rows in place; their life cycle is 4.4's.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

VERSION = "0058_mb_cache_fill_confirmations"
TABLE = "mb_cache_fill_confirmations"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


@dataclass(frozen=True)
class Name:
    id: Any


def log_name(conn: Conn, original: str) -> Name:
    row = conn.execute(
        """INSERT INTO broadcast_artists (original_name, normalized_name)
           VALUES (%s, %s) RETURNING id""",
        (original, original.lower()),
    ).fetchone()
    assert row is not None
    return Name(row["id"])


def queue(conn: Conn, name_id: object, normalized_title: str, title: str) -> None:
    conn.execute(
        f"INSERT INTO {TABLE} (broadcast_artist_id, normalized_title, title) VALUES (%s, %s, %s)",
        (name_id, normalized_title, title),
    )


def test_0058_is_applied(conn: Conn) -> None:
    row = conn.execute(
        "SELECT 1 AS found FROM schema_migrations WHERE version = %s", (VERSION,)
    ).fetchone()
    assert row is not None


def test_the_columns(conn: Conn) -> None:
    rows = conn.execute(
        """SELECT column_name, data_type, is_nullable FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = %s""",
        (TABLE,),
    ).fetchall()
    assert {(r["column_name"], r["data_type"], r["is_nullable"]) for r in rows} == {
        ("id", "uuid", "NO"),
        ("broadcast_artist_id", "uuid", "NO"),
        ("normalized_title", "text", "NO"),
        ("title", "text", "NO"),
        ("queued_at", "timestamp with time zone", "NO"),
    }


def test_a_pair_is_queued_once_and_stamped(conn: Conn) -> None:
    kansas = log_name(conn, "KANSAS")
    queue(conn, kansas.id, "dust in the wind", "Dust in the Wind")
    row = conn.execute(f"SELECT id, queued_at FROM {TABLE}").fetchone()
    assert row is not None and row["id"] is not None and row["queued_at"] is not None

    with pytest.raises(psycopg.errors.UniqueViolation):
        queue(conn, kansas.id, "dust in the wind", "DUST IN THE WIND")


def test_deleting_a_name_deletes_its_queued_pairs(conn: Conn) -> None:
    kansas = log_name(conn, "KANSAS")
    boston = log_name(conn, "BOSTON")
    queue(conn, kansas.id, "dust in the wind", "Dust in the Wind")
    queue(conn, boston.id, "more than a feeling", "More Than a Feeling")

    conn.execute("DELETE FROM broadcast_artists WHERE id = %s", (kansas.id,))

    rows = conn.execute(f"SELECT broadcast_artist_id FROM {TABLE}").fetchall()
    assert [r["broadcast_artist_id"] for r in rows] == [boston.id]
    rule = conn.execute(
        """SELECT c.confdeltype FROM pg_constraint c
           WHERE c.contype = 'f' AND c.conrelid = %s::regclass
             AND c.confrelid = 'broadcast_artists'::regclass""",
        (TABLE,),
    ).fetchone()
    assert rule is not None and rule["confdeltype"] == "c"
