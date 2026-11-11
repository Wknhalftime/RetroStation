"""Performances and their credits, migration 0051 (performances phase 3a PR 2, SPEC S1, D1; design
C40, C44, C71; AUD-R032).

S1 adds:

- ``performances``: ``id``, ``work_id`` (not null), ``recording_mbid``, ``version_type`` (not
  null: the file-side reader's ``VersionType``), ``length_ms`` and ``preferred_copy_id``. One
  performance per (work, recording MBID) where the MBID is present; ``UNIQUE (id, work_id)``
  for the files' composite key;
- ``performance_credits``: ``performance_id``, ``position``, ``artist_id`` and ``mbid`` (both
  nullable, at least one set; D1), ``credited_name`` (question A of the lock request),
  ``join_after`` (the joining words after the entry) and ``role`` (``main`` or ``featured``).
  The primary key is (performance, position);
- ``library_files.performance_id``, with ``(performance_id, work_id)`` referencing
  ``performances (id, work_id)``, ``DEFERRABLE INITIALLY IMMEDIATE``, and a CHECK that a file
  with a performance has a work (MATCH SIMPLE would skip a NULL work);
- ``matches.performance_id``, a nullable reference;
- ``(preferred_copy_id, id)`` referencing ``library_files (id, performance_id)``, so a
  performance's preferred copy is one of its own files.

The delete rules: a work's performances go with it (cascade), and their credits with them; a
deleted performance leaves its matches with no performance; a deleted file that was a preferred
copy leaves the performance with none (``ON DELETE SET NULL (preferred_copy_id)``). A
performance a file still holds can't be deleted, and a file that is a preferred copy can't leave
its performance until the copy is re-picked (S3's order).

``backend/db/rollback_0051_performances.sql`` undoes 0051; its test runs it inside one
transaction that is rolled back, so the test database keeps 0051.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0051_performances"
MIGRATION = ROOT / "backend" / "db" / "migrations" / f"{VERSION}.sql"
ROLLBACK = ROOT / "backend" / "db" / f"rollback_{VERSION}.sql"
MBID = "11111111-1111-1111-1111-111111111111"
OTHER_MBID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


# --- seeds and reads ------------------------------------------------------------------------------


def file_on(conn: Conn, work_id: str | None) -> UUID:
    """A present file whose own ``work_id`` is *work_id*."""
    file_id = seed.library_file(conn)
    conn.execute("UPDATE library_files SET work_id = %s WHERE id = %s", (work_id, file_id))
    return file_id


def performance(conn: Conn, work_id: str, recording_mbid: str | None = None) -> UUID:
    row = conn.execute(
        """INSERT INTO performances (work_id, recording_mbid, version_type, length_ms)
           VALUES (%s, %s, 'original', 200000) RETURNING id""",
        (work_id, recording_mbid),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def hold(conn: Conn, file_id: UUID, performance_id: UUID | None) -> None:
    conn.execute(
        "UPDATE library_files SET performance_id = %s WHERE id = %s", (performance_id, file_id)
    )


def prefer(conn: Conn, performance_id: UUID, file_id: UUID | None) -> None:
    conn.execute(
        "UPDATE performances SET preferred_copy_id = %s WHERE id = %s", (file_id, performance_id)
    )


def credit(
    conn: Conn,
    performance_id: UUID,
    position: int,
    *,
    artist_id: str | None = None,
    mbid: str | None = MBID,
    role: str = "main",
) -> None:
    conn.execute(
        """INSERT INTO performance_credits (performance_id, position, artist_id, mbid, role)
           VALUES (%s, %s, %s, %s, %s)""",
        (performance_id, position, artist_id, mbid, role),
    )


def column(conn: Conn, table: str, name: str) -> tuple[str, str] | None:
    """(data_type, is_nullable) of a column, or None when it doesn't exist."""
    row = conn.execute(
        """SELECT data_type, is_nullable FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = %s AND column_name = %s""",
        (table, name),
    ).fetchone()
    return None if row is None else (row["data_type"], row["is_nullable"])


def indexes(conn: Conn, table: str) -> set[tuple[str, ...]]:
    """The column lists of every index on *table*, in index order."""
    rows = conn.execute(
        """SELECT array_agg(a.attname ORDER BY k.n) AS columns
           FROM pg_index i
           CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, n)
           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
           WHERE i.indrelid = %s::regclass
           GROUP BY i.indexrelid""",
        (table,),
    ).fetchall()
    return {tuple(r["columns"]) for r in rows}


def an_artist(conn: Conn) -> str:
    row = conn.execute("SELECT artist_id FROM works WHERE id = %s", (seed.work(conn),)).fetchone()
    assert row is not None
    return str(row["artist_id"])


# --- the tables -----------------------------------------------------------------------------------


def test_the_performances_table(conn: Conn) -> None:
    assert column(conn, "performances", "id") == ("uuid", "NO")
    assert column(conn, "performances", "work_id") == ("text", "NO")
    assert column(conn, "performances", "recording_mbid") == ("text", "YES")
    assert column(conn, "performances", "version_type") == ("text", "NO")
    assert column(conn, "performances", "length_ms") == ("integer", "YES")
    assert column(conn, "performances", "preferred_copy_id") == ("uuid", "YES")


def test_the_credits_table(conn: Conn) -> None:
    assert column(conn, "performance_credits", "performance_id") == ("uuid", "NO")
    assert (column(conn, "performance_credits", "position") or ("", ""))[1] == "NO"
    assert column(conn, "performance_credits", "artist_id") == ("text", "YES")
    assert column(conn, "performance_credits", "mbid") == ("text", "YES")
    assert column(conn, "performance_credits", "credited_name") == ("text", "YES")
    assert column(conn, "performance_credits", "join_after") == ("text", "YES")
    assert column(conn, "performance_credits", "role") == ("text", "NO")


def test_files_and_matches_gain_a_performance(conn: Conn) -> None:
    assert column(conn, "library_files", "performance_id") == ("uuid", "YES")
    assert column(conn, "matches", "performance_id") == ("uuid", "YES")


def test_one_performance_per_recording_mbid_in_a_work(conn: Conn) -> None:
    """Unique where the MBID is present: performances without one never collide, and another
    work may hold the same MBID (3b builds the global index)."""
    work, other = seed.work(conn), seed.work(conn)
    performance(conn, work, MBID)
    performance(conn, work)
    performance(conn, work)
    performance(conn, other, MBID)
    performance(conn, work, OTHER_MBID)

    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        performance(conn, work, MBID)

    count = conn.execute("SELECT count(*) AS n FROM performances").fetchone()
    assert count is not None and count["n"] == 5


def test_a_performance_needs_a_work_and_a_version(conn: Conn) -> None:
    work = seed.work(conn)
    with pytest.raises(psycopg.errors.NotNullViolation), conn.transaction():
        conn.execute("INSERT INTO performances (work_id, version_type) VALUES (NULL, 'live')")
    with pytest.raises(psycopg.errors.NotNullViolation), conn.transaction():
        conn.execute("INSERT INTO performances (work_id, version_type) VALUES (%s, NULL)", (work,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            "INSERT INTO performances (work_id, version_type) VALUES ('no-such-work', 'live')"
        )


# --- a file and its performance share a work ------------------------------------------------------


def test_a_file_holds_a_performance_of_its_own_work(conn: Conn) -> None:
    work, other = seed.work(conn), seed.work(conn)
    file_id = file_on(conn, work)
    theirs = performance(conn, other)
    ours = performance(conn, work)

    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        hold(conn, file_id, theirs)
    hold(conn, file_id, ours)

    row = conn.execute(
        "SELECT performance_id FROM library_files WHERE id = %s", (file_id,)
    ).fetchone()
    assert row is not None and UUID(str(row["performance_id"])) == ours


def test_moving_a_file_without_its_performance_fails_at_the_statement(conn: Conn) -> None:
    """E4: the composite key is checked per statement, so a writer that changes ``work_id`` and
    keeps ``performance_id`` fails; one that clears it in the same UPDATE succeeds."""
    work, other = seed.work(conn), seed.work(conn)
    file_id = file_on(conn, work)
    hold(conn, file_id, performance(conn, work))

    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("UPDATE library_files SET work_id = %s WHERE id = %s", (other, file_id))
    conn.execute(
        "UPDATE library_files SET work_id = %s, performance_id = NULL WHERE id = %s",
        (other, file_id),
    )


def test_a_file_with_a_performance_has_a_work(conn: Conn) -> None:
    """MATCH SIMPLE skips a NULL ``work_id``, so the CHECK keeps the pair whole."""
    work = seed.work(conn)
    file_id = file_on(conn, work)
    hold(conn, file_id, performance(conn, work))

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute("UPDATE library_files SET work_id = NULL WHERE id = %s", (file_id,))


def test_the_files_key_is_deferrable_initially_immediate(conn: Conn) -> None:
    rows = conn.execute(
        """SELECT c.condeferrable, c.condeferred, c.confrelid::regclass::text AS target
           FROM pg_constraint c
           WHERE c.conrelid = 'library_files'::regclass AND c.contype = 'f'
             AND c.conkey @> ARRAY[
                 (SELECT attnum FROM pg_attribute
                  WHERE attrelid = 'library_files'::regclass AND attname = 'performance_id')
             ]::smallint[]"""
    ).fetchall()

    assert [(r["condeferrable"], r["condeferred"], r["target"]) for r in rows] == [
        (True, False, "performances")
    ]


# --- the preferred copy ---------------------------------------------------------------------------


def test_a_preferred_copy_is_one_of_its_performances_files(conn: Conn) -> None:
    work = seed.work(conn)
    ours, theirs = performance(conn, work, MBID), performance(conn, work, OTHER_MBID)
    copy, elsewhere, loose = file_on(conn, work), file_on(conn, work), file_on(conn, work)
    hold(conn, copy, ours)
    hold(conn, elsewhere, theirs)

    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        prefer(conn, ours, elsewhere)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        prefer(conn, ours, loose)
    prefer(conn, ours, copy)

    row = conn.execute("SELECT preferred_copy_id FROM performances WHERE id = %s", (ours,))
    got = row.fetchone()
    assert got is not None and UUID(str(got["preferred_copy_id"])) == copy


def test_a_preferred_copy_cant_leave_before_it_is_re_picked(conn: Conn) -> None:
    """S3's order: re-pick (or clear) the copy, then move the file."""
    work, other = seed.work(conn), seed.work(conn)
    held = performance(conn, work)
    copy = file_on(conn, work)
    hold(conn, copy, held)
    prefer(conn, held, copy)

    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            "UPDATE library_files SET work_id = %s, performance_id = NULL WHERE id = %s",
            (other, copy),
        )
    prefer(conn, held, None)
    conn.execute(
        "UPDATE library_files SET work_id = %s, performance_id = NULL WHERE id = %s",
        (other, copy),
    )


# --- credits --------------------------------------------------------------------------------------


def test_a_credit_names_an_artist_or_an_mbid(conn: Conn) -> None:
    """D1: an MBID with no artist is credited by MBID; a credit naming neither is refused."""
    held = performance(conn, seed.work(conn))
    artist = an_artist(conn)
    credit(conn, held, 0, artist_id=artist, mbid=None)
    credit(conn, held, 1, artist_id=None, mbid=MBID)
    credit(conn, held, 2, artist_id=artist, mbid=OTHER_MBID, role="featured")

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        credit(conn, held, 3, artist_id=None, mbid=None)
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        credit(conn, held, 1)
    with pytest.raises((psycopg.IntegrityError, psycopg.DataError)), conn.transaction():
        credit(conn, held, 4, role="producer")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        credit(conn, held, 5, artist_id="no-such-artist")


# --- delete rules ---------------------------------------------------------------------------------


def test_a_works_performances_and_credits_go_with_it(conn: Conn) -> None:
    gone, kept = seed.work(conn), seed.work(conn)
    for work_id in (gone, kept):
        credit(conn, performance(conn, work_id, MBID), 0)

    conn.execute("DELETE FROM works WHERE id = %s", (gone,))

    rows = conn.execute("SELECT work_id FROM performances").fetchall()
    credits = conn.execute("SELECT count(*) AS n FROM performance_credits").fetchone()
    assert [r["work_id"] for r in rows] == [kept]
    assert credits is not None and credits["n"] == 1


def test_a_deleted_performance_leaves_its_matches_with_none(conn: Conn) -> None:
    work = seed.work(conn)
    held = performance(conn, work)
    credit(conn, held, 0)
    line = seed.identity(conn)
    match = seed.match(conn, line, file_on(conn, work))
    conn.execute("UPDATE matches SET performance_id = %s WHERE id = %s", (held, match))

    conn.execute("DELETE FROM performances WHERE id = %s", (held,))

    row = conn.execute("SELECT performance_id FROM matches WHERE id = %s", (match,)).fetchone()
    credits = conn.execute("SELECT count(*) AS n FROM performance_credits").fetchone()
    assert row is not None and row["performance_id"] is None
    assert credits is not None and credits["n"] == 0


def test_a_performance_a_file_holds_stays(conn: Conn) -> None:
    work = seed.work(conn)
    held = performance(conn, work)
    hold(conn, file_on(conn, work), held)

    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("DELETE FROM performances WHERE id = %s", (held,))


def test_a_deleted_preferred_copy_leaves_its_performance_with_none(conn: Conn) -> None:
    work = seed.work(conn)
    held = performance(conn, work)
    copy, other = file_on(conn, work), file_on(conn, work)
    hold(conn, copy, held)
    hold(conn, other, held)
    prefer(conn, held, copy)

    conn.execute("DELETE FROM library_files WHERE id = %s", (copy,))

    row = conn.execute(
        "SELECT id, preferred_copy_id FROM performances WHERE id = %s", (held,)
    ).fetchone()
    assert row is not None and row["preferred_copy_id"] is None


# --- indexes and the migration's habits -----------------------------------------------------------


def test_the_indexes(conn: Conn) -> None:
    assert ("performance_id", "work_id") in indexes(conn, "library_files")
    assert ("id", "performance_id") in indexes(conn, "library_files")
    assert ("performance_id",) in indexes(conn, "matches")
    assert {("work_id",), ("preferred_copy_id",), ("id", "work_id")} <= indexes(
        conn, "performances"
    )
    assert {("artist_id",), ("mbid",)} <= indexes(conn, "performance_credits")


def test_the_migration_waits_briefly_for_locks_and_analyzes() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "SET LOCAL lock_timeout" in sql
    for table in ("performances", "performance_credits", "library_files", "matches"):
        assert f"ANALYZE {table};" in sql


# --- the rollback ---------------------------------------------------------------------------------


@contextmanager
def before_0051(url: str) -> Iterator[Conn]:
    """The test database without 0051, in one transaction rolled back at the end."""
    conn = psycopg.connect(url, row_factory=dict_row)
    try:
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_the_rollback_undoes_0051(migrated_db: str) -> None:
    """The rollback leaves 0050's schema, with performances held and stored: no tables, no
    columns, and no 0051 in ``schema_migrations``."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as setup:
        work = seed.work(setup)
        held = performance(setup, work, MBID)
        credit(setup, held, 0)
        copy = file_on(setup, work)
        hold(setup, copy, held)
        prefer(setup, held, copy)
        match = seed.match(setup, seed.identity(setup), copy)
        setup.execute("UPDATE matches SET performance_id = %s WHERE id = %s", (held, match))

    with before_0051(migrated_db) as conn:
        tables = conn.execute(
            """SELECT to_regclass('public.performances') AS p,
                      to_regclass('public.performance_credits') AS c"""
        ).fetchone()
        applied = conn.execute(
            "SELECT count(*) AS n FROM schema_migrations WHERE version = %s", (VERSION,)
        ).fetchone()
        files = conn.execute("SELECT count(*) AS n FROM library_files").fetchone()

        assert tables is not None and (tables["p"], tables["c"]) == (None, None)
        assert column(conn, "library_files", "performance_id") is None
        assert column(conn, "matches", "performance_id") is None
        assert applied is not None and applied["n"] == 0
        assert files is not None and files["n"] == 1
