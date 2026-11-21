"""D1's run-once step: every performance with no credit credits its work's artist (work merges
phase 3b PR 2, SPEC D1, D17 (scope Q3, Q5); design §5 Work, §6.9; AUD-R078), on PostgreSQL.

After 3a's build has finished (its done-marker), the task gives every performance that has no
credit one credit, its work's artist from 0055's seed (3b PR 5b, D38; a work names no artist
since S7), dropping the seed with its marker (D49): position 0, the artist's id, its
MBID if it has one, its name as the credited name, no joining words, ``main``. A performance
whose work's artist is a placeholder (``SPECIAL_PURPOSE_MBIDS``) stays without one (Q5, as 3a).
A performance with any credit is left as it is.

It makes no MusicBrainz call and moves no file: afterwards a re-scan of an untagged file with
unchanged tags (3a's D6) still finds it fits the performance it holds. It commits as it goes,
sets its own done-marker when done, and a run after that does nothing. It reports how many
performances it credited, on its own progress type, ``work_artist_credits``. Nothing queues it:
the cutover and the gate run it by name, after the build and before any merge (the planner
waits for its marker, PR 4).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from backend.config import get_settings
from backend.domain.library import PERFORMANCES_BUILT_SETTING, WORK_ARTISTS_CREDITED_SETTING
from backend.services.performance_service import reassign_retagged
from backend.services.repository_factory import RepositoryFactory
from backend.services.song_match_service import SongMatchService
from backend.tasks import huey_app, work_artist_credit_tasks
from backend.tasks.work_artist_credit_tasks import credit_work_artists_task
from tests.integration import work_artist_seed_table as seed_table
from tests.integration.work_artist_seed_table import seed_table_kept  # noqa: F401

# D1's step drops 0055's seed with its marker (D49): the table is put back after each test.
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("seed_table_kept")]

type Conn = psycopg.Connection[DictRow]

BON_JOVI_MBID = "0a000000-0000-4000-8000-0000000000c1"
NETTLES_MBID = "0a000000-0000-4000-8000-0000000000c2"
VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"  # MusicBrainz's Various Artists


@pytest.fixture
def conn(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Conn]:
    """The test's connection. The settings name the test database, so the task's own connections
    use it, and nothing it might queue leaves the test. 3a's build counts as done."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setattr(huey_app.huey, "enqueue", lambda _task: None)
    get_settings.cache_clear()
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        set_marker(connection, PERFORMANCES_BUILT_SETTING)
        yield connection
    get_settings.cache_clear()


def run(conn: Conn) -> dict[str, Any]:
    """One run of the task, with the test's work committed first."""
    conn.commit()
    result: dict[str, Any] = credit_work_artists_task.call_local()
    conn.rollback()
    return result


# --- seeds and reads ------------------------------------------------------------------------------


def set_marker(conn: Conn, key: str) -> None:
    conn.execute(
        "INSERT INTO user_settings (key, value) VALUES (%s, '2026-11-15T00:00:00+00:00')", (key,)
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


def work(conn: Conn, artist_id: str, title: str = "Always") -> str:
    """A work whose artist 0055's seed holds, while the seed is there."""
    work_id = f"work-{title.lower()}-{artist_id}"
    conn.execute("INSERT INTO works (id, title) VALUES (%s, %s)", (work_id, title))
    if seed_table.exists(conn):
        seed_table.seed(conn, work_id, artist_id)
    return work_id


def performance(conn: Conn, work_id: str, *, length_ms: int = 200_000) -> UUID:
    """A performance of *work_id* with no credit, as 3a's build left an untagged one."""
    row = conn.execute(
        """INSERT INTO performances (work_id, version_type, length_ms)
           VALUES (%s, 'original', %s) RETURNING id""",
        (work_id, length_ms),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def tag_credit(conn: Conn, performance_id: UUID, mbid: str, name: str) -> None:
    conn.execute(
        """INSERT INTO performance_credits (performance_id, position, mbid, credited_name, role)
           VALUES (%s, 0, %s, %s, 'main')""",
        (performance_id, mbid, name),
    )


def held_file(
    conn: Conn, work_id: str, performance_id: UUID, n: int, *, credit: str | None = None
) -> UUID:
    """File number *n* on *work_id*, held by *performance_id*, untagged unless *credit* (an
    MBID) is given."""
    file_id = UUID(int=n)
    conn.execute(
        """INSERT INTO library_files
               (id, file_path, file_status, work_id, performance_id, track_title, duration_ms,
                artist_mbids, credit)
           VALUES (%s, %s, 'present', %s, %s, 'Always', 200000, %s, %s)""",
        (
            file_id,
            f"D:/music/{n}.flac",
            work_id,
            performance_id,
            None if credit is None else [credit],
            None if credit is None else Jsonb([{"mbid": credit, "name": None, "join": None}]),
        ),
    )
    return file_id


def credits(conn: Conn, performance_id: UUID) -> list[tuple[Any, ...]]:
    """(position, artist_id, mbid, credited_name, join_after, role) of the performance."""
    rows = conn.execute(
        """SELECT position, artist_id, mbid, credited_name, join_after, role
           FROM performance_credits WHERE performance_id = %s ORDER BY position""",
        (performance_id,),
    ).fetchall()
    return [
        (r["position"], r["artist_id"], r["mbid"], r["credited_name"], r["join_after"], r["role"])
        for r in rows
    ]


def count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()  # noqa: S608
    assert row is not None
    return int(row["n"])


# --- the credit -----------------------------------------------------------------------------------


def test_every_uncredited_performance_credits_its_works_artist(conn: Conn) -> None:
    """Each performance credits its own work's artist, by id with its MBID, or by id alone for
    a name-only artist."""
    bon_jovi = artist(conn, "Bon Jovi", BON_JOVI_MBID)
    poison = artist(conn, "Poison")
    song = work(conn, bon_jovi)
    always, also = performance(conn, song), performance(conn, song, length_ms=260_000)
    nothin = performance(conn, work(conn, poison, "Nothin' But A Good Time"))

    result = run(conn)

    assert credits(conn, always) == [(0, bon_jovi, BON_JOVI_MBID, "Bon Jovi", None, "main")]
    assert credits(conn, also) == [(0, bon_jovi, BON_JOVI_MBID, "Bon Jovi", None, "main")]
    assert credits(conn, nothin) == [(0, poison, None, "Poison", None, "main")]
    assert result == {"done": True, "credited": 3}


def test_a_placeholder_work_artist_gives_no_credit(conn: Conn) -> None:
    various = artist(conn, "Various Artists", VARIOUS)
    held = performance(conn, work(conn, various))

    result = run(conn)

    assert credits(conn, held) == []
    assert result == {"done": True, "credited": 0}


def test_a_credited_performance_is_left_as_it_is(conn: Conn) -> None:
    """A tag credit (here Nettles on Bon Jovi's work) keeps its one credit."""
    bon_jovi = artist(conn, "Bon Jovi", BON_JOVI_MBID)
    duet = performance(conn, work(conn, bon_jovi))
    tag_credit(conn, duet, NETTLES_MBID, "Jennifer Nettles")

    result = run(conn)

    assert credits(conn, duet) == [(0, None, NETTLES_MBID, "Jennifer Nettles", None, "main")]
    assert result == {"done": True, "credited": 0}


# --- once, after the build ------------------------------------------------------------------------


def test_it_waits_for_the_build(conn: Conn) -> None:
    conn.execute("DELETE FROM user_settings WHERE key = %s", (PERFORMANCES_BUILT_SETTING,))
    held = performance(conn, work(conn, artist(conn, "Poison")))

    result = run(conn)

    assert result == {"done": False, "credited": 0}
    assert credits(conn, held) == []
    assert marker(conn, WORK_ARTISTS_CREDITED_SETTING) is None


def test_it_runs_once(conn: Conn) -> None:
    """Its marker ends it: a performance left uncredited later is no longer its business
    (``assign_performance`` credits new ones)."""
    poison = artist(conn, "Poison")
    performance(conn, work(conn, poison))
    first = run(conn)
    later = performance(conn, work(conn, poison, "Unskinny Bop"))

    second = run(conn)

    assert first == {"done": True, "credited": 1}
    assert marker(conn, WORK_ARTISTS_CREDITED_SETTING) is not None
    assert second == {"done": True, "credited": 0}
    assert credits(conn, later) == []


def test_batches_commit_as_they_go(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """Five performances in batches of two: every one is reached."""
    monkeypatch.setattr(work_artist_credit_tasks, "BATCH_SIZE", 2)
    poison = artist(conn, "Poison")
    held = [performance(conn, work(conn, poison, f"Song {n}")) for n in range(5)]

    result = run(conn)

    assert result == {"done": True, "credited": 5}
    assert all(credits(conn, p) == [(0, poison, None, "Poison", None, "main")] for p in held)


def test_no_file_moves_afterwards(conn: Conn) -> None:
    """3a's D6: a re-scan of the untagged copies (and of a tagged one) moves nothing."""
    bon_jovi = artist(conn, "Bon Jovi", BON_JOVI_MBID)
    song = work(conn, bon_jovi)
    untagged = performance(conn, song)
    tagged = performance(conn, song)
    tag_credit(conn, tagged, NETTLES_MBID, "Jennifer Nettles")
    files = [
        held_file(conn, song, untagged, 1),
        held_file(conn, song, untagged, 2),
        held_file(conn, song, tagged, 3, credit=NETTLES_MBID),
    ]
    run(conn)

    repos = RepositoryFactory(conn)
    moved = reassign_retagged(
        [f"D:/music/{n}.flac" for n in (1, 2, 3)],
        repos.performances,
        SongMatchService(repos.matches),
    )

    assert moved == 0
    held = conn.execute(
        "SELECT id, performance_id FROM library_files WHERE id = ANY(%s) ORDER BY id", (files,)
    ).fetchall()
    assert [UUID(str(r["performance_id"])) for r in held] == [untagged, untagged, tagged]


def test_it_reports_on_its_own_progress_type(conn: Conn) -> None:
    performance(conn, work(conn, artist(conn, "Poison")))

    run(conn)

    row = conn.execute(
        "SELECT status FROM progress_tracking WHERE task_type = 'work_artist_credits'"
    ).fetchone()
    assert row is not None and row["status"] == "completed"
    assert count(conn, "performance_credits") == 1
