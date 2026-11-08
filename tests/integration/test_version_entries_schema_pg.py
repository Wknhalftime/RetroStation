"""Version entries and their states, migration 0050 (versions phase 2b, SPEC S2, S9; design
C60, C62, D3; AUD-R034).

S2: a version entry is a ``format_overrides`` row beside the format rows (C60):

- ``version_text`` is new and nullable, ``format_name`` becomes nullable, and a CHECK requires
  exactly one of the two;
- a plain ``UNIQUE (work_id, version_text)`` sits beside ``UNIQUE (work_id, format_name)``; NULLs
  are distinct, so format rows never collide on their NULL version text, nor version rows on
  their NULL format;
- an insert that names only a format still works, so the locked tests that write format
  overrides pass unchanged. Two texts may share one file ("(Live)" and "(Lv)", design 6.8).

``version_entry_states`` holds what a mapping can't: one row per (work, version text), a
``state`` that is ``dismissed`` or NULL, and the ``seen_at`` it was last seen. Its ``work_id``
references ``works`` ``ON DELETE CASCADE`` (D3).

S9, silenced at start: the migration writes a state row, ``seen_at`` = now and no state, for
every (work, version text) on a matched line when it runs. A matched line is the view's (SPEC
context): an ``auto_matched`` or ``manual_matched`` line, its best match with a file (highest
``confidence_score``, then ``created_at``, then ``id``), and that file's work, else its
recording's work. A line counts whether or not it has plays.

S9's test runs on the database as it was before 0050: ``backend/db/rollback_0050_version_entries
.sql`` undoes 0050 inside one transaction, the test seeds the lines and runs 0050's SQL, and the
transaction is rolled back at the end, so the test database keeps 0050.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0050_version_entries"
MIGRATION = ROOT / "backend" / "db" / "migrations" / f"{VERSION}.sql"
ROLLBACK = ROOT / "backend" / "db" / f"rollback_{VERSION}.sql"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def file_on(conn: Conn, work_id: str | None) -> UUID:
    """A present file whose own ``work_id`` is *work_id*."""
    file_id = seed.library_file(conn)
    conn.execute("UPDATE library_files SET work_id = %s WHERE id = %s", (work_id, file_id))
    return file_id


def version_entry(conn: Conn, work_id: str, text: str, file_id: UUID) -> UUID:
    row = conn.execute(
        """INSERT INTO format_overrides (work_id, version_text, preferred_file_id)
           VALUES (%s, %s, %s) RETURNING id""",
        (work_id, text, file_id),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def column(conn: Conn, table: str, name: str) -> tuple[str, str] | None:
    """(data_type, is_nullable) of a column, or None when it doesn't exist."""
    row = conn.execute(
        """SELECT data_type, is_nullable FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = %s AND column_name = %s""",
        (table, name),
    ).fetchone()
    return None if row is None else (row["data_type"], row["is_nullable"])


# --- S2: format_overrides -------------------------------------------------------------------------


def test_format_overrides_gain_a_nullable_version_text(conn: Conn) -> None:
    assert column(conn, "format_overrides", "version_text") == ("text", "YES")
    assert column(conn, "format_overrides", "format_name") == ("text", "YES")


def test_an_insert_naming_only_a_format_still_works(conn: Conn) -> None:
    work = seed.work(conn)
    seed.format_override(conn, work, "AC", file_on(conn, work))

    [row] = conn.execute(
        "SELECT format_name, version_text FROM format_overrides WHERE work_id = %s", (work,)
    ).fetchall()

    assert (row["format_name"], row["version_text"]) == ("AC", None)


def test_a_version_entry_names_no_format(conn: Conn) -> None:
    work = seed.work(conn)
    entry = version_entry(conn, work, "live", file_on(conn, work))

    row = conn.execute(
        "SELECT format_name, version_text FROM format_overrides WHERE id = %s", (entry,)
    ).fetchone()

    assert row is not None and (row["format_name"], row["version_text"]) == (None, "live")


def test_exactly_one_of_format_and_version_text(conn: Conn) -> None:
    work = seed.work(conn)
    file_id = file_on(conn, work)

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO format_overrides (work_id, preferred_file_id) VALUES (%s, %s)",
            (work, file_id),
        )
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            """INSERT INTO format_overrides (work_id, format_name, version_text, preferred_file_id)
               VALUES (%s, 'AC', 'live', %s)""",
            (work, file_id),
        )


def test_one_entry_per_work_and_version_text(conn: Conn) -> None:
    work, other = seed.work(conn), seed.work(conn)
    live, lv = file_on(conn, work), file_on(conn, work)
    version_entry(conn, work, "live", live)

    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        version_entry(conn, work, "live", lv)
    version_entry(conn, work, "lv", live)  # two texts may share one file
    version_entry(conn, other, "live", file_on(conn, other))

    count = conn.execute("SELECT count(*) AS n FROM format_overrides").fetchone()
    assert count is not None and count["n"] == 3


def test_format_rows_and_version_rows_never_collide(conn: Conn) -> None:
    """NULLs are distinct: format rows share a NULL version text, version rows a NULL format."""
    work = seed.work(conn)
    file_id = file_on(conn, work)
    seed.format_override(conn, work, "AC", file_id)
    seed.format_override(conn, work, "Rock", file_id)
    version_entry(conn, work, "live", file_id)
    version_entry(conn, work, "acoustic", file_id)

    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        seed.format_override(conn, work, "AC", file_id)

    count = conn.execute(
        "SELECT count(*) AS n FROM format_overrides WHERE work_id = %s", (work,)
    ).fetchone()
    assert count is not None and count["n"] == 4


# --- S2: version_entry_states ---------------------------------------------------------------------


def test_the_states_table(conn: Conn) -> None:
    assert column(conn, "version_entry_states", "work_id") == ("text", "NO")
    assert column(conn, "version_entry_states", "version_text") == ("text", "NO")
    assert column(conn, "version_entry_states", "state") == ("text", "YES")
    assert column(conn, "version_entry_states", "seen_at") == ("timestamp with time zone", "NO")


def test_a_state_is_dismissed_or_none_once_per_work_and_text(conn: Conn) -> None:
    work = seed.work(conn)
    insert = """INSERT INTO version_entry_states (work_id, version_text, state, seen_at)
                VALUES (%s, %s, %s, %s)"""
    conn.execute(insert, (work, "live", "dismissed", T0))
    conn.execute(insert, (work, "acoustic", None, T0))

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(insert, (work, "demo", "mapped", T0))
    with pytest.raises(psycopg.errors.NotNullViolation), conn.transaction():
        conn.execute(insert, (work, "demo", None, None))
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        conn.execute(insert, (work, "live", None, T0))
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(insert, ("no-such-work", "live", None, T0))


def test_a_works_states_go_with_it(conn: Conn) -> None:
    """D3: a state is no reason to keep a work, and the database removes it with its work."""
    work, kept = seed.work(conn), seed.work(conn)
    for work_id in (work, kept):
        conn.execute(
            """INSERT INTO version_entry_states (work_id, version_text, state, seen_at)
               VALUES (%s, 'live', 'dismissed', %s)""",
            (work_id, T0),
        )

    conn.execute("DELETE FROM works WHERE id = %s", (work,))

    rows = conn.execute("SELECT work_id FROM version_entry_states").fetchall()
    assert [r["work_id"] for r in rows] == [kept]


# --- S9: silenced at start ------------------------------------------------------------------------


@contextmanager
def before_0050(url: str) -> Iterator[Conn]:
    """The test database without 0050, in one transaction rolled back at the end."""
    conn = psycopg.connect(url, row_factory=dict_row)
    try:
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        yield conn
    finally:
        conn.rollback()
        conn.close()


def matched_line(conn: Conn, title: str, file_id: UUID, *, status: str = "auto_matched") -> UUID:
    line = seed.identity(conn, title=title, status=status)
    seed.match(conn, line, file_id)
    return line


def states(conn: Conn) -> dict[tuple[str, str], tuple[str | None, datetime]]:
    rows = conn.execute(
        "SELECT work_id, version_text, state, seen_at FROM version_entry_states"
    ).fetchall()
    return {(r["work_id"], r["version_text"]): (r["state"], r["seen_at"]) for r in rows}


def test_existing_entries_start_seen(migrated_db: str) -> None:
    with before_0050(migrated_db) as conn:
        live_work, rec_work, best_work = seed.work(conn), seed.work(conn), seed.work(conn)
        live_file = file_on(conn, live_work)
        played = matched_line(conn, "Enter Sandman (Live)", live_file)
        seed.play(conn, seed.playlist(conn, seed.station(conn)), played, seed.at("08:00"))
        matched_line(conn, "Enter Sandman (LIVE)", live_file)  # the same pair again
        matched_line(conn, "Enter Sandman (Live S&M)", live_file, status="manual_matched")
        matched_line(conn, "Enter Sandman", live_file)  # no version text
        matched_line(conn, "Enter Sandman (Demo)", live_file, status="needs_review")
        matched_line(conn, "Enter Sandman (Lv)", live_file, status="pending")

        # A file with no work of its own: its recording's work.
        on_recording = seed.library_file(conn, recording_id=seed.recording(conn, rec_work))
        matched_line(conn, "Black (Acoustic)", on_recording)

        # Two matches: the best (highest confidence) decides the work.
        both = seed.identity(conn, title="Black (Demo)")
        seed.match(conn, both, live_file, confidence=0.5)
        seed.match(conn, both, file_on(conn, best_work), confidence=0.95)

        matched_line(conn, "Black (Remix)", file_on(conn, None))  # a file on no work
        fileless = seed.identity(conn, title="Black (Extended)")
        seed.match(conn, fileless, None)  # a match without a file

        seed.format_override(conn, live_work, "AC", live_file)  # an existing override

        conn.execute(MIGRATION.read_text(encoding="utf-8"))
        now = conn.execute("SELECT now() AS now").fetchone()
        assert now is not None
        override = conn.execute(
            "SELECT format_name, version_text, preferred_file_id FROM format_overrides"
        ).fetchall()
        filled = conn.execute(
            "SELECT version_text FROM track_identities WHERE id = %s", (played,)
        ).fetchone()

        assert states(conn) == {
            (live_work, "live"): (None, now["now"]),
            (live_work, "live s&m"): (None, now["now"]),
            (rec_work, "acoustic"): (None, now["now"]),
            (best_work, "demo"): (None, now["now"]),
        }
        assert [(r["format_name"], r["version_text"]) for r in override] == [("AC", None)]
        assert filled is not None and filled["version_text"] == "live"


def test_the_rollback_undoes_0050(migrated_db: str) -> None:
    """The rollback leaves 0049's schema: no version text on lines or overrides, every
    override a format row again (a version entry goes, a format row stays), no states table,
    the view without a version step, and no 0050 in ``schema_migrations``."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as setup:
        work = seed.work(setup)
        file_id = file_on(setup, work)
        seed.format_override(setup, work, "AC", file_id)
        version_entry(setup, work, "live", file_id)

    with before_0050(migrated_db) as conn:
        overrides = conn.execute("SELECT format_name FROM format_overrides").fetchall()
        view = conn.execute("SELECT pg_get_viewdef('play_file_resolution') AS d").fetchone()
        applied = conn.execute(
            "SELECT count(*) AS n FROM schema_migrations WHERE version = %s", (VERSION,)
        ).fetchone()
        states_table = conn.execute(
            "SELECT to_regclass('public.version_entry_states') AS t"
        ).fetchone()

        assert column(conn, "track_identities", "version_text") is None
        assert column(conn, "format_overrides", "version_text") is None
        assert column(conn, "format_overrides", "format_name") == ("text", "NO")
        assert [r["format_name"] for r in overrides] == ["AC"]
        assert states_table is not None and states_table["t"] is None
        assert view is not None and "version" not in view["d"]
        assert applied is not None and applied["n"] == 0
