"""The retirement of ``works.artist_id`` and ``matches.work_id``, D38's holding table and D49's
drop, on PostgreSQL (work merges phase 3b PR 5b, SPEC S7, D38, D49; C70; AUD-R082, AUD-R083,
AUD-R088).

Migration 0055 first copies each work's (id, artist_id) into ``work_artist_seed`` (``work_id``
its key, CASCADE with the work; ``artist_id`` an artist), then drops ``works.artist_id`` with
``idx_works_artist``, and ``matches.work_id`` with ``idx_matches_work_id``. At the cutover every
migration runs before 3a's build and D1's step, so the seed is how they still know each existing
work's artist (D38):

- 3a's build credits a new performance whose first file has no credit with its work's seeded
  artist, none for a placeholder or a work with no seed row;
- D1's step credits each performance with no credit with its work's seeded artist, then drops
  the seed in the transaction that sets its marker (D49); a re-run does nothing.

Nothing else reads the seed: the build reads it through ``files_waiting``, D1's step through
``uncredited`` and drops it through ``drop_seed``. The drop's rollback restores
``works.artist_id`` (nullable) from the seed when it is still there, else from the work's default
performance's first credit, and ``matches.work_id`` from each match's file's work, with both
indexes; it drops the seed and forgets 0055.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.config import get_settings
from backend.domain.library import (
    ARTIST_CREDITS_BACKFILLED_SETTING,
    PERFORMANCES_BUILT_SETTING,
    WORK_ARTISTS_CREDITED_SETTING,
)
from backend.tasks import huey_app
from backend.tasks.performance_build_tasks import build_performances_task
from backend.tasks.work_artist_credit_tasks import credit_work_artists_task
from tests.integration import work_artist_seed_table as seed_table
from tests.integration.work_artist_seed_table import seed_table_kept  # noqa: F401 (fixture)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("seed_table_kept")]

type Conn = psycopg.Connection[DictRow]

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0055_work_artists_from_credits"
POISON_MBID = "0a000000-0000-4000-8000-0000000000d1"
VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"  # MusicBrainz's Various Artists


@pytest.fixture
def conn(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Conn]:
    """The test's connection; the tasks' own connections use the test database too, and nothing
    they might queue leaves the test."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setattr(huey_app.huey, "enqueue", lambda _task: None)
    get_settings.cache_clear()
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection
    get_settings.cache_clear()


@contextmanager
def before_0055(url: str) -> Iterator[Conn]:
    """The test database as it was before 0055, in one transaction rolled back at the end."""
    conn = psycopg.connect(url, row_factory=dict_row)
    try:
        conn.execute(seed_table.ROLLBACK.read_text(encoding="utf-8"))
        yield conn
    finally:
        conn.rollback()
        conn.close()


@contextmanager
def rolled_back(url: str) -> Iterator[Conn]:
    """A transaction on the migrated test database, rolled back at the end."""
    conn = psycopg.connect(url, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


# --- seeds and reads ------------------------------------------------------------------------------


def set_marker(conn: Conn, key: str) -> None:
    conn.execute(
        "INSERT INTO user_settings (key, value) VALUES (%s, '2026-11-21T00:00:00+00:00')", (key,)
    )


def marker(conn: Conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM user_settings WHERE key = %s", (key,)).fetchone()
    return None if row is None else str(row["value"])


def artist(conn: Conn, name: str, mbid: str | None = None) -> str:
    artist_id = f"artist-{name.lower().replace(' ', '-')}"
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, mbid) VALUES (%s, %s, %s, %s)",
        (artist_id, name, name, mbid),
    )
    return artist_id


def work(conn: Conn, title: str) -> str:
    """A work as 0055 leaves them: no artist of its own."""
    work_id = f"work-{title.lower().replace(' ', '-')}"
    conn.execute("INSERT INTO works (id, title) VALUES (%s, %s)", (work_id, title))
    return work_id


def old_work(conn: Conn, title: str, artist_id: str) -> str:
    """A work as it was before 0055, naming its artist."""
    work_id = f"work-{title.lower().replace(' ', '-')}"
    conn.execute(
        "INSERT INTO works (id, title, artist_id) VALUES (%s, %s, %s)", (work_id, title, artist_id)
    )
    return work_id


def untagged_file(conn: Conn, work_id: str, n: int) -> UUID:
    """File number *n* on *work_id*, with no credit and no performance yet."""
    file_id = UUID(int=n)
    conn.execute(
        """INSERT INTO library_files (id, file_path, file_status, work_id, track_title,
                                      duration_ms)
           VALUES (%s, %s, 'present', %s, 'Talk Dirty To Me', 200000)""",
        (file_id, f"D:/music/{n}.flac", work_id),
    )
    return file_id


def performance(conn: Conn, work_id: str) -> UUID:
    row = conn.execute(
        """INSERT INTO performances (work_id, version_type, length_ms)
           VALUES (%s, 'original', 200000) RETURNING id""",
        (work_id,),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def credit(
    conn: Conn, performance_id: UUID, artist_id: str | None, mbid: str | None = None
) -> None:
    conn.execute(
        """INSERT INTO performance_credits (performance_id, position, artist_id, mbid, role)
           VALUES (%s, (SELECT count(*) FROM performance_credits WHERE performance_id = %s),
                   %s, %s, 'main')""",
        (performance_id, performance_id, artist_id, mbid),
    )


def held_file(conn: Conn, work_id: str, performance_id: UUID, n: int) -> UUID:
    file_id = untagged_file(conn, work_id, n)
    conn.execute(
        "UPDATE library_files SET performance_id = %s WHERE id = %s", (performance_id, file_id)
    )
    return file_id


def master(conn: Conn, work_id: str, file_id: UUID) -> None:
    conn.execute(
        """INSERT INTO song_masters (work_id, preferred_file_id, selection_method)
           VALUES (%s, %s, 'auto')""",
        (work_id, file_id),
    )


def credits_of(conn: Conn, file_id: UUID) -> list[tuple[Any, ...]]:
    """(position, artist_id, mbid, credited_name, role) of the file's performance."""
    rows = conn.execute(
        """SELECT c.position, c.artist_id, c.mbid, c.credited_name, c.role
           FROM performance_credits c JOIN library_files f ON f.performance_id = c.performance_id
           WHERE f.id = %s ORDER BY c.position""",
        (file_id,),
    ).fetchall()
    return [(r["position"], r["artist_id"], r["mbid"], r["credited_name"], r["role"]) for r in rows]


def performance_credits(conn: Conn, performance_id: UUID) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        """SELECT position, artist_id, mbid, credited_name, role FROM performance_credits
           WHERE performance_id = %s ORDER BY position""",
        (performance_id,),
    ).fetchall()
    return [(r["position"], r["artist_id"], r["mbid"], r["credited_name"], r["role"]) for r in rows]


def has_column(conn: Conn, table: str, column: str) -> bool:
    row = conn.execute(
        """SELECT EXISTS (SELECT 1 FROM information_schema.columns
                          WHERE table_schema = 'public' AND table_name = %s
                            AND column_name = %s) AS present""",
        (table, column),
    ).fetchone()
    return bool(row and row["present"])


def has_index(conn: Conn, name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (name,)).fetchone()
    return bool(row and row["present"])


def run_build(conn: Conn) -> dict[str, Any]:
    conn.commit()
    result: dict[str, Any] = build_performances_task.call_local()
    conn.rollback()
    return result


def run_d1(conn: Conn) -> dict[str, Any]:
    conn.commit()
    result: dict[str, Any] = credit_work_artists_task.call_local()
    conn.rollback()
    return result


# --- migration 0055 -------------------------------------------------------------------------------


def test_0055_copies_each_works_artist_into_the_seed_then_drops_the_columns(
    migrated_db: str,
) -> None:
    with before_0055(migrated_db) as conn:
        poison = artist(conn, "Poison")
        various = artist(conn, "Various Artists", VARIOUS)
        talk = old_work(conn, "Talk Dirty To Me", poison)
        marmalade = old_work(conn, "Lady Marmalade", various)

        conn.execute(seed_table.MIGRATION.read_text(encoding="utf-8"))

        rows = conn.execute(
            "SELECT work_id, artist_id FROM work_artist_seed ORDER BY work_id"
        ).fetchall()
        assert [(r["work_id"], r["artist_id"]) for r in rows] == [
            (marmalade, various),
            (talk, poison),
        ]
        assert not has_column(conn, "works", "artist_id")
        assert not has_index(conn, "idx_works_artist")
        assert not has_column(conn, "matches", "work_id")
        assert not has_index(conn, "idx_matches_work_id")


def test_the_seed_goes_with_its_work(conn: Conn) -> None:
    poison = artist(conn, "Poison")
    talk = work(conn, "Talk Dirty To Me")
    seed_table.seed(conn, talk, poison)

    conn.execute("DELETE FROM works WHERE id = %s", (talk,))

    row = conn.execute("SELECT count(*) AS n FROM work_artist_seed").fetchone()
    assert row is not None and row["n"] == 0


# --- 3a's build reads it --------------------------------------------------------------------------


def test_the_build_credits_a_new_untagged_performance_with_the_seeded_artist(conn: Conn) -> None:
    poison = artist(conn, "Poison", POISON_MBID)
    talk = work(conn, "Talk Dirty To Me")
    seed_table.seed(conn, talk, poison)
    untagged = untagged_file(conn, talk, 1)
    set_marker(conn, ARTIST_CREDITS_BACKFILLED_SETTING)

    result = run_build(conn)

    assert result == {"done": True, "files": 1}
    assert credits_of(conn, untagged) == [(0, poison, POISON_MBID, "Poison", "main")]


def test_the_build_gives_no_credit_for_a_placeholder_or_a_work_with_no_seed_row(
    conn: Conn,
) -> None:
    various = artist(conn, "Various Artists", VARIOUS)
    marmalade = work(conn, "Lady Marmalade")
    seed_table.seed(conn, marmalade, various)
    compilation = untagged_file(conn, marmalade, 1)
    unseeded = untagged_file(conn, work(conn, "Every Rose Has Its Thorn"), 2)
    set_marker(conn, ARTIST_CREDITS_BACKFILLED_SETTING)

    run_build(conn)

    assert credits_of(conn, compilation) == []
    assert credits_of(conn, unseeded) == []


# --- D1's step reads it, then drops it (D49) ------------------------------------------------------


def test_d1s_step_credits_from_the_seed_then_drops_it_with_its_marker(conn: Conn) -> None:
    poison = artist(conn, "Poison")
    talk = work(conn, "Talk Dirty To Me")
    seed_table.seed(conn, talk, poison)
    held = performance(conn, talk)
    set_marker(conn, PERFORMANCES_BUILT_SETTING)

    result = run_d1(conn)

    assert result == {"done": True, "credited": 1}
    assert performance_credits(conn, held) == [(0, poison, None, "Poison", "main")]
    assert marker(conn, WORK_ARTISTS_CREDITED_SETTING) is not None
    assert not seed_table.exists(conn)


def test_d1s_step_run_again_does_nothing(conn: Conn) -> None:
    poison = artist(conn, "Poison")
    talk = work(conn, "Talk Dirty To Me")
    seed_table.seed(conn, talk, poison)
    performance(conn, talk)
    set_marker(conn, PERFORMANCES_BUILT_SETTING)
    run_d1(conn)
    later = performance(conn, work(conn, "Unskinny Bop"))

    again = run_d1(conn)

    assert again == {"done": True, "credited": 0}
    assert performance_credits(conn, later) == []
    assert not seed_table.exists(conn)


def test_d1s_step_waiting_for_the_build_keeps_the_seed(conn: Conn) -> None:
    poison = artist(conn, "Poison")
    talk = work(conn, "Talk Dirty To Me")
    seed_table.seed(conn, talk, poison)
    held = performance(conn, talk)

    result = run_d1(conn)

    assert result == {"done": False, "credited": 0}
    assert performance_credits(conn, held) == []
    assert seed_table.exists(conn)


# --- nothing else reads it ------------------------------------------------------------------------


def _python_files(*parts: str) -> list[Path]:
    return sorted((ROOT / "backend").joinpath(*parts).rglob("*.py"))


def test_only_the_build_and_d1s_step_read_the_seed() -> None:
    """The table is named only by 0055, its rollback and the performances repository; its three
    reads are called only by the two run-once tasks (ports and adapters aside)."""
    named = sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "backend").rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".sql"}
        and seed_table.TABLE in p.read_text(encoding="utf-8")
    )
    assert named == [
        "backend/db/migrations/0055_work_artists_from_credits.sql",
        "backend/db/repositories/performances.py",
        "backend/db/rollback_0055_work_artists_from_credits.sql",
    ]
    callers: dict[str, set[str]] = {"files_waiting": set(), "uncredited": set(), "drop_seed": set()}
    for path in _python_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("backend/repositories/", "backend/db/repositories/")):
            continue
        text = path.read_text(encoding="utf-8")
        for method, found in callers.items():
            if re.search(rf"\.{method}\(", text):
                found.add(rel)
    assert callers == {
        "files_waiting": {"backend/tasks/performance_build_tasks.py"},
        "uncredited": {"backend/tasks/work_artist_credit_tasks.py"},
        "drop_seed": {"backend/tasks/work_artist_credit_tasks.py"},
    }


# --- the rollback ---------------------------------------------------------------------------------


def test_the_rollback_restores_the_artist_from_the_seed_else_the_default_credit(
    migrated_db: str,
) -> None:
    """Seeded works take their seed row; a work made after 0055 takes the first credit of the
    performance holding its master's file (by artist id, or the artist holding a credit's MBID);
    a work with neither is left with none."""
    with rolled_back(migrated_db) as conn:
        poison = artist(conn, "Poison")
        nettles = artist(conn, "Jennifer Nettles", POISON_MBID)
        sugarland = artist(conn, "Sugarland")
        seeded = work(conn, "Talk Dirty To Me")
        seed_table.seed(conn, seeded, poison)
        later = work(conn, "Stuck Like Glue")
        default = performance(conn, later)
        credit(conn, default, sugarland)
        other = performance(conn, later)
        credit(conn, other, poison)
        master(conn, later, held_file(conn, later, default, 1))
        held_file(conn, later, other, 2)
        by_mbid = work(conn, "Something More")
        tagged = performance(conn, by_mbid)
        credit(conn, tagged, None, POISON_MBID)
        master(conn, by_mbid, held_file(conn, by_mbid, tagged, 3))
        nobody = work(conn, "Silence")

        conn.execute(seed_table.ROLLBACK.read_text(encoding="utf-8"))

        rows = conn.execute("SELECT id, artist_id FROM works ORDER BY id").fetchall()
        assert {r["id"]: r["artist_id"] for r in rows} == {
            seeded: poison,
            later: sugarland,
            by_mbid: nettles,
            nobody: None,
        }
        assert not seed_table.exists(conn)
        assert has_index(conn, "idx_works_artist")
        forgotten = conn.execute(
            "SELECT count(*) AS n FROM schema_migrations WHERE version = %s", (VERSION,)
        ).fetchone()
        assert forgotten is not None and forgotten["n"] == 0


def test_the_rollback_after_d1s_drop_reads_the_default_credit(migrated_db: str) -> None:
    with rolled_back(migrated_db) as conn:
        poison = artist(conn, "Poison")
        talk = work(conn, "Talk Dirty To Me")
        held = performance(conn, talk)
        credit(conn, held, poison)
        master(conn, talk, held_file(conn, talk, held, 1))
        conn.execute("DROP TABLE work_artist_seed")

        conn.execute(seed_table.ROLLBACK.read_text(encoding="utf-8"))

        row = conn.execute("SELECT artist_id FROM works WHERE id = %s", (talk,)).fetchone()
        assert row is not None and row["artist_id"] == poison


def test_the_rollback_restores_each_matchs_work_from_its_file(migrated_db: str) -> None:
    with rolled_back(migrated_db) as conn:
        talk = work(conn, "Talk Dirty To Me")
        file_id = untagged_file(conn, talk, 1)
        name = conn.execute(
            """INSERT INTO broadcast_artists (original_name, normalized_name)
               VALUES ('POISON', 'poison') RETURNING id"""
        ).fetchone()
        assert name is not None
        line = conn.execute(
            """INSERT INTO track_identities (broadcast_artist_id, original_title, normalized_title,
                                             normalized_signature, match_status)
               VALUES (%s, 'TALK DIRTY', 'talk dirty', 'sig-talk-dirty', 'auto_matched')
               RETURNING id""",
            (name["id"],),
        ).fetchone()
        assert line is not None
        conn.execute(
            "INSERT INTO matches (identity_id, library_file_id) VALUES (%s, %s)",
            (line["id"], file_id),
        )

        conn.execute(seed_table.ROLLBACK.read_text(encoding="utf-8"))

        row = conn.execute("SELECT work_id FROM matches").fetchone()
        assert row is not None and row["work_id"] == talk
        assert has_index(conn, "idx_matches_work_id")
