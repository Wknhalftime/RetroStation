"""Air play with merge pins: ``play_file_resolution`` after 0053 (artist model phase 3b PR 3,
SPEC S12, S3's schema, D4; design C61, C72, C56; AUD-R033's 3b check, AUD-R079).

A matched line plays the first of these that has an entry for the line's work:

1. the performance its best match stores (its preferred copy) -- ``source`` ``performance``;
2. the version entry for (work, the line's version text) -- ``version``;
3. the format override for the station's format -- ``override``;
4. the file its best match pins (``matches.pinned_file_id``) -- ``pin``;
5. the song master -- ``master``.

A pin stands in for the master only (C72), so it comes after the override and the entry. It is
read from the best-match lateral the view already has, and its file through the existing file
join: no new select or join, so the locked view tests stand. A pin on the master's own file
takes the normal path (``master``). A missing pinned file is reported, never fallen back from.

``matches.pinned_file_id`` is a nullable foreign key to ``library_files`` with no ON DELETE
action (D4: every path that deletes a file releases its pins first, and one that forgets fails),
and is indexed. ``backend/db/rollback_0053_merge_pins.sql`` puts back 0052's view and drops the
column; its test runs it inside one transaction that is rolled back.

Pins are seeded here by plain SQL, as a merge would leave them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.integration import credit_picks_seed as cp
from tests.integration import work_merge_seed as seed
from tests.integration.credit_picks_seed import Conn

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0053_merge_pins"
MIGRATION = ROOT / "backend" / "db" / "migrations" / f"{VERSION}.sql"
ROLLBACK = ROOT / "backend" / "db" / f"rollback_{VERSION}.sql"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


class Pinned:
    """A work whose master is a flac, with another copy (``pinned``, an mp3) a merge pinned."""

    def __init__(self, conn: Conn) -> None:
        self.work = cp.work(conn, "Lady Marmalade")
        self.master = cp.copy(conn, self.work, title="Lady Marmalade")
        self.pinned = cp.copy(conn, self.work, fmt="mp3", title="Lady Marmalade")
        seed.master(conn, self.work, self.master)

    def line(self, conn: Conn, at: UUID, title: str = "Lady Marmalade") -> UUID:
        """A line on the master, heard at *at*, its best match pinning ``pinned``."""
        identity_id = seed.heard(conn, self.master, at, title=title)
        seed.pin(conn, identity_id, self.pinned)
        return identity_id


@pytest.fixture
def world(conn: Conn) -> Pinned:
    return Pinned(conn)


# --- the order ------------------------------------------------------------------------------------


def test_no_override_the_pin_plays(conn: Conn, world: Pinned) -> None:
    pinned_line = world.line(conn, seed.station(conn))
    plain_line = seed.heard(conn, world.master, seed.station(conn))

    assert seed.playing(conn, pinned_line) == {(world.pinned, "pin")}
    assert seed.playing(conn, plain_line) == {(world.master, "master")}


def test_an_override_plays_the_override(conn: Conn, world: Pinned) -> None:
    """C72: the pin stands in for the master only, so the station's format override wins; at
    a station of another format the pin plays."""
    ac_edit = cp.copy(conn, world.work, fmt="mp3", title="Lady Marmalade")
    cp.override(conn, world.work, "AC", ac_edit)
    ac, chr_ = seed.station(conn, "AC"), seed.station(conn, "CHR")
    line = world.line(conn, ac)
    seed.also_heard(conn, line, chr_)

    assert seed.playing(conn, line, ac) == {(ac_edit, "override")}
    assert seed.playing(conn, line, chr_) == {(world.pinned, "pin")}


def test_a_mapped_entry_plays_the_entry(conn: Conn, world: Pinned) -> None:
    live = cp.copy(conn, world.work, fmt="mp3", title="Lady Marmalade (Live)")
    cp.version_entry(conn, world.work, "live", live)

    line = world.line(conn, seed.station(conn), "Lady Marmalade (Live)")

    assert seed.playing(conn, line) == {(live, "version")}


def test_a_stored_performance_plays_before_the_pin(conn: Conn, world: Pinned) -> None:
    """A line that stores a non-default performance plays its preferred copy (3a, C61)."""
    cp.performance(conn, world.work, [world.master], credit=[cp.artist(conn, "LaBelle")])
    duet_copy = cp.copy(conn, world.work, fmt="mp3", title="Lady Marmalade")
    duet = cp.performance(
        conn,
        world.work,
        [duet_copy],
        credit=[cp.artist(conn, "Patti LaBelle")],
        preferred=duet_copy,
    )
    line = world.line(conn, seed.station(conn))
    cp.store(conn, line, duet)

    assert seed.playing(conn, line) == {(duet_copy, "performance")}


def test_a_pin_on_the_masters_own_file_takes_the_normal_path(conn: Conn, world: Pinned) -> None:
    line = world.line(conn, seed.station(conn))
    seed.pin(conn, line, world.master)

    assert seed.playing(conn, line) == {(world.master, "master")}


def test_a_missing_pinned_file_is_reported(conn: Conn, world: Pinned) -> None:
    """The pin step wins and its missing file is reported; the master doesn't stand in."""
    line = world.line(conn, seed.station(conn))
    seed.missing(conn, world.pinned)

    assert seed.playing(conn, line) == {(world.pinned, "pin")}
    assert seed.statuses(conn, line) == {"missing"}


def test_only_the_best_match_counts(conn: Conn, world: Pinned) -> None:
    """A pin on a match that is not the line's best match plays nothing."""
    at = seed.station(conn)
    line = seed.heard(conn, world.master, at)
    other = cp.also_match(conn, line, world.pinned, confidence=0.5)
    conn.execute("UPDATE matches SET pinned_file_id = %s WHERE id = %s", (world.pinned, other))

    assert seed.playing(conn, line) == {(world.master, "master")}
    assert seed.pin_of(conn, line) is None


def test_an_unmatched_line_plays_nothing(conn: Conn, world: Pinned) -> None:
    line = cp.line(conn, "Lady Marmalade", world.master, status="needs_review")
    cp.plays(conn, line, station_id=seed.station(conn))
    seed.pin(conn, line, world.pinned)

    assert seed.playing(conn, line) == {(None, None)}


# --- S3, D4: the column ---------------------------------------------------------------------------


def test_the_pin_column(conn: Conn) -> None:
    """Nullable, a foreign key to ``library_files`` with no ON DELETE action, and indexed."""
    column = conn.execute(
        """SELECT data_type, is_nullable FROM information_schema.columns
           WHERE table_name = 'matches' AND column_name = 'pinned_file_id'"""
    ).fetchone()
    keys = conn.execute(
        """SELECT confrelid::regclass::text AS refers, confdeltype
           FROM pg_constraint c
           JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
           WHERE c.conrelid = 'matches'::regclass AND c.contype = 'f'
             AND a.attname = 'pinned_file_id'"""
    ).fetchall()
    indexed = conn.execute(
        """SELECT count(*) AS n FROM pg_index i
           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
           WHERE i.indrelid = 'matches'::regclass AND a.attname = 'pinned_file_id'"""
    ).fetchone()

    assert column is not None
    assert (column["data_type"], column["is_nullable"]) == ("uuid", "YES")
    assert [(k["refers"], k["confdeltype"]) for k in keys] == [("library_files", "a")]
    assert indexed is not None and indexed["n"] >= 1


# --- AUD-R033: keys only --------------------------------------------------------------------------


def test_the_view_only_looks_up_keys(conn: Conn) -> None:
    """The pin comes from the existing best-match subquery and plays through the existing file
    join: still two selects, one join of ``performances`` and two of ``format_overrides``; no
    parsing; the same columns in the same order."""
    row = conn.execute("SELECT pg_get_viewdef('play_file_resolution') AS d").fetchone()
    assert row is not None
    definition = " ".join(row["d"].lower().split())
    columns = conn.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_name = 'play_file_resolution' ORDER BY ordinal_position"""
    ).fetchall()

    assert definition.count("select") == 2
    assert definition.count("join performances") == 1
    assert definition.count("join format_overrides") == 2
    assert definition.count("join library_files") == 3
    assert re.search(r"\bm\.pinned_file_id\b", definition)
    assert "'pin'" in definition
    for reads in ("performance_credits", "log_name_links", "artists", "work_merges"):
        assert reads not in definition, reads
    for parsing in ("regexp", "substring", "lower(", "split_part", "position(", "like "):
        assert parsing not in definition, parsing
    assert [c["column_name"] for c in columns] == [
        "play_event_id",
        "station_id",
        "matched_file_id",
        "work_id",
        "file_id",
        "file_status",
        "source",
    ]


def test_the_migration_waits_briefly_for_locks_and_analyzes() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "SET LOCAL lock_timeout" in sql
    assert "ANALYZE matches;" in sql


# --- the rollback ---------------------------------------------------------------------------------


@contextmanager
def before_0053(url: str) -> Iterator[Conn]:
    """The test database without 0053, in one transaction rolled back at the end."""
    conn = psycopg.connect(url, row_factory=dict_row)
    try:
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_the_rollback_undoes_0053(migrated_db: str) -> None:
    """0052's view again (performance, version, override, master), no pin column, no 0053 in
    ``schema_migrations``; a pinned line plays its master again."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as setup:
        world = Pinned(setup)
        line = world.line(setup, seed.station(setup))

    with before_0053(migrated_db) as conn:
        definition = conn.execute("SELECT pg_get_viewdef('play_file_resolution') AS d").fetchone()
        column = conn.execute(
            """SELECT count(*) AS n FROM information_schema.columns
               WHERE table_name = 'matches' AND column_name = 'pinned_file_id'"""
        ).fetchone()
        applied = conn.execute(
            "SELECT count(*) AS n FROM schema_migrations WHERE version = %s", (VERSION,)
        ).fetchone()

        assert definition is not None
        assert "pinned_file_id" not in definition["d"]
        assert "performances" in definition["d"]
        assert column is not None and column["n"] == 0
        assert applied is not None and applied["n"] == 0
        assert seed.playing(conn, line) == {(world.master, "master")}
