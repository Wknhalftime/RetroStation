"""Air play with stored performances: ``play_file_resolution`` after 0052 (performances phase 3a
PR 3, SPEC S7, E7, E8's view side; design C61, C71, C56; AUD-R033's 3a check).

A matched line plays the first of these that has an entry for the line's work:

1. the performance its best match stores -- its preferred copy, ``source`` ``performance``;
2. the version entry for (work, the line's version text) -- ``version``;
3. the format override for the station's format -- ``override``;
4. the song master -- ``master``.

The stored performance is read from the best match the view already picks (no new subquery)
and key-joined to ``performances`` on its id and the line's work, so a performance of another
work never plays. It is skipped when it is the work's default performance (the performance of
the master's file, C71), or when it has no preferred copy, and the next step plays. Credit
before version (E7, Lance 2026-10-29). When the winning step's file is unavailable the view
reports it and never falls back (E8).

AUD-R033: the view only looks up keys and keeps its columns.
``backend/db/rollback_0052_credit_picks.sql`` puts back 0050's view and drops 0052's table; its
test runs it inside one transaction that is rolled back.
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

from tests.integration import credit_picks_seed as seed
from tests.integration.credit_picks_seed import Conn, Duet

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0052_credit_picks"
MIGRATION = ROOT / "backend" / "db" / "migrations" / f"{VERSION}.sql"
ROLLBACK = ROOT / "backend" / "db" / f"rollback_{VERSION}.sql"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


@pytest.fixture
def duet(conn: Conn) -> Duet:
    world = Duet(conn)
    conn.execute(
        "UPDATE performances SET preferred_copy_id = %s WHERE id = %s",
        (world.duet_flac, world.duet),
    )
    return world


def plays(conn: Conn, identity_id: UUID) -> set[tuple[UUID | None, str | None]]:
    return seed.resolved(conn, identity_id)


def statuses(conn: Conn, identity_id: UUID) -> set[str | None]:
    rows = conn.execute(
        """SELECT pfr.file_status FROM play_file_resolution pfr
           JOIN play_events pe ON pe.id = pfr.play_event_id
           WHERE pe.identity_id = %s""",
        (identity_id,),
    ).fetchall()
    return {r["file_status"] for r in rows}


# --- the order ------------------------------------------------------------------------------------


def test_a_stored_performance_plays_its_preferred_copy(conn: Conn, duet: Duet) -> None:
    stored_line, plain_line = duet.duet_line(conn), duet.solo_line(conn)
    seed.store(conn, stored_line, duet.duet)

    assert plays(conn, stored_line) == {(duet.duet_flac, "performance")}
    assert plays(conn, plain_line) == {(duet.master, "master")}


def test_credit_beats_version(conn: Conn) -> None:
    """E7: a Korn/Amy Lee "Freak On A Leash (Unplugged)" line that stores the duet plays it,
    though "unplugged" is mapped; once the duet is the default, the entry plays (C71)."""
    korn, amy_lee = seed.artist(conn, "Korn"), seed.artist(conn, "Amy Lee")
    work = seed.work(conn, "Freak On A Leash", korn)
    studio = seed.copy(conn, work)
    seed.performance(conn, work, [studio], credit=[korn])
    seed.master(conn, work, studio)
    unplugged_korn = seed.copy(conn, work)
    seed.performance(conn, work, [unplugged_korn], credit=[korn], recording_mbid=str(UUID(int=7)))
    seed.version_entry(conn, work, "unplugged", unplugged_korn)
    duet_copy = seed.copy(conn, work)
    duet = seed.performance(conn, work, [duet_copy], credit=[korn, amy_lee], preferred=duet_copy)
    name = seed.log_name(conn, "KORN/AMY LEE", korn, amy_lee)
    line = seed.line(conn, "Freak On A Leash (Unplugged)", studio, name=name)
    seed.plays(conn, line)
    seed.store(conn, line, duet)

    before = plays(conn, line)
    seed.master(conn, work, duet_copy)
    after = plays(conn, line)

    assert before == {(duet_copy, "performance")}
    assert after == {(unplugged_korn, "version")}


def test_credit_beats_format(conn: Conn, duet: Duet) -> None:
    """A station whose format has an override still plays the stored performance."""
    ac_copy = seed.copy(conn, duet.work)
    seed.performance(conn, duet.work, [ac_copy], credit=[duet.bon_jovi], recording_mbid="r-ac")
    seed.override(conn, duet.work, "AC", ac_copy)
    at_ac = seed.station(conn, "WAAC", "AC")
    stored_line = seed.line(conn, "Who Says You Can't Go Home", duet.master, name=duet.duet_name)
    seed.plays(conn, stored_line, station_id=at_ac)
    plain_line = seed.line(conn, "Who Says You Can't Go Home", duet.master, name=duet.solo_name)
    seed.plays(conn, plain_line, station_id=at_ac)
    seed.store(conn, stored_line, duet.duet)

    assert plays(conn, stored_line) == {(duet.duet_flac, "performance")}
    assert plays(conn, plain_line) == {(ac_copy, "override")}


def test_the_default_is_skipped(conn: Conn, duet: Duet) -> None:
    """C71: a stored performance that is the work's default plays as the default does: the
    master, not the performance's preferred copy."""
    line = duet.duet_line(conn)
    seed.store(conn, line, duet.duet)
    seed.master(conn, duet.work, duet.duet_mp3)

    assert plays(conn, line) == {(duet.duet_mp3, "master")}


def test_the_default_skipped_lets_the_version_entry_play(conn: Conn, duet: Duet) -> None:
    live = seed.copy(conn, duet.work)
    seed.performance(conn, duet.work, [live], credit=[duet.bon_jovi], recording_mbid="r-live")
    seed.version_entry(conn, duet.work, "live", live)
    line = duet.duet_line(conn, "Who Says You Can't Go Home (Live)")
    seed.store(conn, line, duet.solo)

    assert plays(conn, line) == {(live, "version")}


def test_a_master_with_no_performance_makes_no_default(conn: Conn) -> None:
    """A master whose file holds no performance never matches a stored one, so it plays."""
    singer = seed.artist(conn, "Singer")
    work = seed.work(conn, "Song", singer)
    master = seed.copy(conn, work)
    seed.master(conn, work, master)
    copy = seed.copy(conn, work)
    held = seed.performance(conn, work, [copy], credit=[singer], preferred=copy)
    line = seed.line(conn, "Song", master, name=seed.log_name(conn, "SINGER", singer))
    seed.plays(conn, line)
    seed.store(conn, line, held)

    assert plays(conn, line) == {(copy, "performance")}


def test_a_work_with_no_master_plays_its_stored_performance(conn: Conn, duet: Duet) -> None:
    conn.execute("DELETE FROM song_masters WHERE work_id = %s", (duet.work,))
    stored_line, plain_line = duet.duet_line(conn), duet.solo_line(conn)
    seed.store(conn, stored_line, duet.duet)

    assert plays(conn, stored_line) == {(duet.duet_flac, "performance")}
    assert plays(conn, plain_line) == {(None, None)}


# --- what is never read ---------------------------------------------------------------------------


def test_another_works_performance_never_plays(conn: Conn, duet: Duet) -> None:
    """A stale performance of another work, left on a match, is ignored (the key join)."""
    other = seed.work(conn, "Livin' On A Prayer", duet.bon_jovi)
    elsewhere = seed.copy(conn, other)
    theirs = seed.performance(conn, other, [elsewhere], credit=[duet.bon_jovi], preferred=elsewhere)
    line = duet.duet_line(conn)
    seed.store(conn, line, theirs)

    assert plays(conn, line) == {(duet.master, "master")}


def test_only_the_best_match_counts(conn: Conn, duet: Duet) -> None:
    """The performance is read from the match the view already picks, and from no other."""
    line = duet.duet_line(conn)
    weaker = seed.also_match(conn, line, duet.duet_mp3, confidence=0.5)
    conn.execute("UPDATE matches SET performance_id = %s WHERE id = %s", (duet.duet, weaker))

    on_weaker = plays(conn, line)
    seed.store(conn, line, duet.duet)

    assert on_weaker == {(duet.master, "master")}
    assert plays(conn, line) == {(duet.duet_flac, "performance")}


def test_a_performance_without_a_preferred_copy_is_no_entry(conn: Conn, duet: Duet) -> None:
    line = duet.duet_line(conn)
    seed.store(conn, line, duet.duet)
    conn.execute("UPDATE performances SET preferred_copy_id = NULL WHERE id = %s", (duet.duet,))

    assert plays(conn, line) == {(duet.master, "master")}


def test_an_unavailable_preferred_copy_is_reported(conn: Conn, duet: Duet) -> None:
    """E8: the step wins and its missing file is reported; the master doesn't stand in."""
    line = duet.duet_line(conn)
    seed.store(conn, line, duet.duet)
    conn.execute(
        "UPDATE library_files SET file_status = 'missing' WHERE id = %s", (duet.duet_flac,)
    )

    assert plays(conn, line) == {(duet.duet_flac, "performance")}
    assert statuses(conn, line) == {"missing"}


def test_an_unmatched_line_plays_nothing(conn: Conn, duet: Duet) -> None:
    line = seed.line(
        conn, "Who Says You Can't Go Home", duet.master, name=duet.duet_name, status="needs_review"
    )
    seed.plays(conn, line)
    seed.store(conn, line, duet.duet)

    assert plays(conn, line) == {(None, None)}


# --- AUD-R033: keys only --------------------------------------------------------------------------


def test_the_view_only_looks_up_keys(conn: Conn) -> None:
    """The stored performance comes from the existing best-match subquery and one key join of
    ``performances``; still two selects and two joins of ``format_overrides``; no parsing; the
    same columns in the same order."""
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
    assert re.search(r"\bm\.performance_id\b", definition)
    assert "'performance'" in definition
    for reads in ("performance_credits", "log_name_links", "artists", "credit_pick_proposals"):
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
    assert "ANALYZE credit_pick_proposals;" in sql


# --- the rollback ---------------------------------------------------------------------------------


@contextmanager
def before_0052(url: str) -> Iterator[Conn]:
    """The test database without 0052, in one transaction rolled back at the end."""
    conn = psycopg.connect(url, row_factory=dict_row)
    try:
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        yield conn
    finally:
        conn.rollback()
        conn.close()


def test_the_rollback_undoes_0052(migrated_db: str) -> None:
    """0050's view again (version, override, master), no proposals table, no 0052 in
    ``schema_migrations``; 0051's performances stay, and a stored one no longer plays."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as setup:
        world = Duet(setup)
        setup.execute(
            "UPDATE performances SET preferred_copy_id = %s WHERE id = %s",
            (world.duet_flac, world.duet),
        )
        line = world.duet_line(setup)
        seed.store(setup, line, world.duet)
        seed.propose(setup, line, world.duet)

    with before_0052(migrated_db) as conn:
        definition = conn.execute("SELECT pg_get_viewdef('play_file_resolution') AS d").fetchone()
        tables = conn.execute(
            """SELECT to_regclass('public.credit_pick_proposals') AS proposals,
                      to_regclass('public.performances') AS performances"""
        ).fetchone()
        applied = conn.execute(
            "SELECT count(*) AS n FROM schema_migrations WHERE version = %s", (VERSION,)
        ).fetchone()

        assert definition is not None and "performances" not in definition["d"]
        assert tables is not None and tables["proposals"] is None
        assert tables["performances"] is not None
        assert applied is not None and applied["n"] == 0
        assert seed.resolved(conn, line) == {(world.master, "master")}
