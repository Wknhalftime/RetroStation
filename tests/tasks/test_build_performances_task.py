"""The run-once build of performances (performances phase 3a PR 2, SPEC S2, E1, E5, D1; design
§6.2, §6.3 step 3, C7, C40, C44, C69), on PostgreSQL.

After 2a's credit backfill has finished (its done-marker), the task gives every file on a work,
present or missing, a performance of that work, in file-id order. A file joins a performance of
its work when:

- it has a recording MBID and the performance has the same one; a file with an MBID joins
  nothing else and otherwise starts its own performance, holding that MBID;
- it has no recording MBID, and the performance (with or without an MBID) has the same credit
  artists, the same version and a length within 3 s (3,000 ms counts) of the performance's
  ``length_ms``. A file with a NULL or negative length never joins this way. Where several
  performances qualify, the closest length wins, then the performance whose first file has the
  lower id.

"The same credit artists" compares sets of MBIDs: the file's from its ``credit``, the
performance's from its credits, placeholders (``SPECIAL_PURPOSE_MBIDS``) left out of both. No
credit, or only placeholders, is the empty set (E1). "The same version" is the file-side
reader's ``VersionType`` of ``track_title`` (no title reads as ``original``).

A new performance takes its first file's recording MBID, version and length, and its credits
from that file's ``credit``, placeholders left out, numbered from 0 in order:

- ``mbid`` is the entry's MBID; ``artist_id`` is the artist holding that MBID, else NULL (D1:
  no stub is made, and readers find the artist by MBID later);
- ``credited_name`` is the entry's name, else the artist's name, else NULL (2a's D9);
- ``join_after`` is the entry's joining words as stored;
- ``role`` is ``featured`` when the joining words before the entry are "feat.", "feat", "ft.",
  "ft" or "featuring" (any case, spaces trimmed), else ``main`` (C69).

The task commits as it goes; a run that stopped resumes with the files still without a
performance, and files done keep theirs (E5). It sets its own done-marker once every file on a
work has a performance, and a run after that does nothing. It reports on its own progress type,
``performances_build``.
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
from backend.domain.catalog import SPECIAL_PURPOSE_MBIDS
from backend.domain.library import (
    ARTIST_CREDITS_BACKFILLED_SETTING,
    PERFORMANCES_BUILT_SETTING,
)
from backend.tasks import huey_app
from backend.tasks.performance_build_tasks import build_performances_task
from tests.integration import stream_seed as seed

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]
type Credit = list[tuple[str, str | None, str | None]]
"""(mbid, name, join) entries of one file's ``credit``."""

ONE = "0a000000-0000-4000-8000-000000000001"
TWO = "0a000000-0000-4000-8000-000000000002"
THREE = "0a000000-0000-4000-8000-000000000003"
RECORDING = "0b000000-0000-4000-8000-000000000001"
OTHER_RECORDING = "0b000000-0000-4000-8000-000000000002"
VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"  # MusicBrainz's Various Artists


def fid(n: int) -> UUID:
    """File ids that sort as their numbers do."""
    return UUID(int=n)


@pytest.fixture
def conn(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Conn]:
    """The test's connection. The settings name the test database, so the task's own connections
    use it, and whatever it hands off stays off the queue. 2a's backfill counts as done."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setattr(huey_app.huey, "enqueue", lambda _task: None)
    get_settings.cache_clear()
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        set_marker(connection, ARTIST_CREDITS_BACKFILLED_SETTING)
        yield connection
    get_settings.cache_clear()


def run(conn: Conn) -> dict[str, Any]:
    """One run of the task, with the test's work committed first."""
    conn.commit()
    result: dict[str, Any] = build_performances_task.call_local()
    conn.rollback()
    return result


# --- seeds and reads ------------------------------------------------------------------------------


def set_marker(conn: Conn, key: str) -> None:
    conn.execute(
        "INSERT INTO user_settings (key, value) VALUES (%s, '2026-11-10T00:00:00+00:00')", (key,)
    )


def marker(conn: Conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM user_settings WHERE key = %s", (key,)).fetchone()
    return None if row is None else str(row["value"])


def file_row(
    conn: Conn,
    work_id: str | None,
    n: int,
    *,
    recording_mbid: str | None = None,
    title: str | None = "Black",
    duration_ms: int | None = 200_000,
    credit: Credit | None = None,
    status: str = "present",
) -> UUID:
    """File number *n* on *work_id*, with these tag columns."""
    file_id = fid(n)
    entries = None if credit is None else [{"mbid": m, "name": a, "join": j} for m, a, j in credit]
    conn.execute(
        """INSERT INTO library_files
               (id, file_path, file_status, work_id, recording_mbid, track_title, duration_ms,
                artist_mbids, credit)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            file_id,
            f"D:/music/{n}.flac",
            status,
            work_id,
            recording_mbid,
            title,
            duration_ms,
            None if credit is None else [m for m, _, _ in credit],
            None if entries is None else Jsonb(entries),
        ),
    )
    return file_id


def artist(conn: Conn, mbid: str, name: str) -> str:
    artist_id = f"artist-{mbid}"
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, mbid) VALUES (%s, %s, %s, %s)",
        (artist_id, name, name, mbid),
    )
    return artist_id


def performance_of(conn: Conn, file_id: UUID) -> UUID | None:
    row = conn.execute(
        "SELECT performance_id FROM library_files WHERE id = %s", (file_id,)
    ).fetchone()
    assert row is not None
    return None if row["performance_id"] is None else UUID(str(row["performance_id"]))


def groups(conn: Conn, work_id: str) -> list[list[UUID]]:
    """The files of each performance of *work_id*, in file-id order, groups by first file."""
    rows = conn.execute(
        """SELECT p.id AS performance_id, f.id AS file_id
           FROM performances p JOIN library_files f ON f.performance_id = p.id
           WHERE p.work_id = %s ORDER BY f.id""",
        (work_id,),
    ).fetchall()
    held: dict[str, list[UUID]] = {}
    for row in rows:
        held.setdefault(str(row["performance_id"]), []).append(UUID(str(row["file_id"])))
    return sorted(held.values())


def performance_row(conn: Conn, file_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        """SELECT p.work_id, p.recording_mbid, p.version_type, p.length_ms, p.preferred_copy_id
           FROM performances p JOIN library_files f ON f.performance_id = p.id
           WHERE f.id = %s""",
        (file_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


def credits_of(conn: Conn, file_id: UUID) -> list[tuple[Any, ...]]:
    """(position, artist_id, mbid, credited_name, join_after, role) of the file's performance."""
    rows = conn.execute(
        """SELECT c.position, c.artist_id, c.mbid, c.credited_name, c.join_after, c.role
           FROM performance_credits c JOIN library_files f ON f.performance_id = c.performance_id
           WHERE f.id = %s ORDER BY c.position""",
        (file_id,),
    ).fetchall()
    return [
        (r["position"], r["artist_id"], r["mbid"], r["credited_name"], r["join_after"], r["role"])
        for r in rows
    ]


def count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()  # noqa: S608
    assert row is not None
    return int(row["n"])


def test_the_placeholder_is_one() -> None:
    assert VARIOUS in SPECIAL_PURPOSE_MBIDS


# --- the identity rule (§6.2) ---------------------------------------------------------------------


def test_the_same_recording_mbid_is_one_performance(conn: Conn) -> None:
    """Whatever the credit, version or length: the first file's fields are the performance's."""
    work = seed.work(conn)
    first = file_row(conn, work, 1, recording_mbid=RECORDING, title="Black", duration_ms=250_000)
    file_row(conn, work, 2, recording_mbid=RECORDING, title="Black (Live)", duration_ms=90_000)
    file_row(conn, work, 3, recording_mbid=RECORDING, credit=[(ONE, "A", None)])

    result = run(conn)

    assert result["files"] == 3
    assert groups(conn, work) == [[fid(1), fid(2), fid(3)]]
    assert performance_row(conn, first) == {
        "work_id": work,
        "recording_mbid": RECORDING,
        "version_type": "original",
        "length_ms": 250_000,
        "preferred_copy_id": None,
    }


def test_another_mbid_or_another_work_is_another_performance(conn: Conn) -> None:
    work, other = seed.work(conn), seed.work(conn)
    file_row(conn, work, 1, recording_mbid=RECORDING)
    file_row(conn, work, 2, recording_mbid=OTHER_RECORDING)
    file_row(conn, other, 3, recording_mbid=RECORDING)

    run(conn)

    assert groups(conn, work) == [[fid(1)], [fid(2)]]
    assert groups(conn, other) == [[fid(3)]]


def test_an_untagged_copy_joins_by_credit_version_and_length(conn: Conn) -> None:
    """A file with no recording MBID joins the performance (here one with an MBID) with the same
    credit artists, version and a length within 3 s of its ``length_ms``."""
    work = seed.work(conn)
    duo = [(ONE, "A", " & "), (TWO, "B", None)]
    file_row(conn, work, 1, recording_mbid=RECORDING, duration_ms=200_000, credit=duo)
    file_row(conn, work, 2, duration_ms=203_000, credit=[(TWO, "B", " & "), (ONE, "A", None)])
    file_row(conn, work, 3, duration_ms=196_999, credit=duo)  # 3,001 ms off
    file_row(conn, work, 4, duration_ms=200_000, credit=[(ONE, "A", None)])  # another credit
    file_row(conn, work, 5, title="Black (Live)", duration_ms=200_000, credit=duo)  # live

    run(conn)

    assert groups(conn, work) == [[fid(1), fid(2)], [fid(3)], [fid(4)], [fid(5)]]
    assert performance_row(conn, fid(5))["version_type"] == "live"
    assert performance_row(conn, fid(3))["recording_mbid"] is None


def test_the_first_files_length_decides(conn: Conn) -> None:
    """Length is measured from the performance's ``length_ms``, the first file's, never from the
    last file that joined."""
    work = seed.work(conn)
    file_row(conn, work, 1, duration_ms=200_000)
    file_row(conn, work, 2, duration_ms=202_500)
    file_row(conn, work, 3, duration_ms=205_000)

    run(conn)

    assert groups(conn, work) == [[fid(1), fid(2)], [fid(3)]]
    assert performance_row(conn, fid(2))["length_ms"] == 200_000


def test_a_file_with_an_mbid_never_joins_by_length(conn: Conn) -> None:
    work = seed.work(conn)
    file_row(conn, work, 1, duration_ms=200_000)
    file_row(conn, work, 2, recording_mbid=RECORDING, duration_ms=200_000)

    run(conn)

    assert groups(conn, work) == [[fid(1)], [fid(2)]]


def test_a_null_or_negative_length_never_joins(conn: Conn) -> None:
    work = seed.work(conn)
    file_row(conn, work, 1, duration_ms=None)
    file_row(conn, work, 2, duration_ms=None)
    file_row(conn, work, 3, duration_ms=-1)
    file_row(conn, work, 4, duration_ms=-1)

    run(conn)

    assert groups(conn, work) == [[fid(1)], [fid(2)], [fid(3)], [fid(4)]]


def test_the_closest_length_wins_then_the_earlier_performance(conn: Conn) -> None:
    work = seed.work(conn)
    file_row(conn, work, 1, recording_mbid=RECORDING, duration_ms=200_000)
    file_row(conn, work, 2, recording_mbid=OTHER_RECORDING, duration_ms=202_000)
    file_row(conn, work, 3, duration_ms=201_500)  # nearer 2
    file_row(conn, work, 4, duration_ms=201_000)  # a tie: 1's performance came first

    run(conn)

    assert groups(conn, work) == [[fid(1), fid(4)], [fid(2), fid(3)]]


def test_missing_files_are_given_one_too(conn: Conn) -> None:
    """Masters may sit on missing files."""
    work = seed.work(conn)
    file_row(conn, work, 1, recording_mbid=RECORDING, status="missing")
    file_row(conn, work, 2, recording_mbid=RECORDING)

    run(conn)

    assert groups(conn, work) == [[fid(1), fid(2)]]


def test_files_on_no_work_get_none_and_an_empty_work_gets_none(conn: Conn) -> None:
    loose = file_row(conn, None, 1, recording_mbid=RECORDING)
    seed.work(conn)

    run(conn)

    assert performance_of(conn, loose) is None
    assert count(conn, "performances") == 0


# --- credits (C44, C69, D1) -----------------------------------------------------------------------


def test_credits_come_from_the_first_file(conn: Conn) -> None:
    work = seed.work(conn)
    a = artist(conn, ONE, "Bon Jovi")
    artist(conn, TWO, "Jennifer Nettles")
    file_row(
        conn,
        work,
        1,
        recording_mbid=RECORDING,
        credit=[(ONE, "Bon Jovi", " feat. "), (TWO, None, " & "), (THREE, "Somebody", None)],
    )
    file_row(conn, work, 2, recording_mbid=RECORDING, credit=[(THREE, "Somebody", None)])

    run(conn)

    assert credits_of(conn, fid(2)) == [
        (0, a, ONE, "Bon Jovi", " feat. ", "main"),
        (1, f"artist-{TWO}", TWO, "Jennifer Nettles", " & ", "featured"),
        (2, None, THREE, "Somebody", None, "main"),
    ]


@pytest.mark.parametrize("join", [" FEAT ", " ft. ", "ft", " Featuring "])
def test_featuring_words(conn: Conn, join: str) -> None:
    work = seed.work(conn)
    file_row(conn, work, 1, credit=[(ONE, "A", join), (TWO, "B", None)])

    run(conn)

    assert [c[5] for c in credits_of(conn, fid(1))] == ["main", "featured"]


def test_an_mbid_with_no_artist_and_no_name_is_credited_by_mbid(conn: Conn) -> None:
    """D1, and 2a's D9: nothing to name it by, and no stub is made."""
    work = seed.work(conn)
    artists_before = count(conn, "artists")
    file_row(conn, work, 1, credit=[(ONE, None, None)])

    run(conn)

    assert credits_of(conn, fid(1)) == [(0, None, ONE, None, None, "main")]
    assert count(conn, "artists") == artists_before


def test_placeholders_are_never_credited(conn: Conn) -> None:
    """C7: the track artist without the placeholder; a credit of placeholders only is empty, so
    the file groups as an untagged one with a file that has no credit (E1). A later file whose
    credit holds a placeholder beside the track artist joins the track artist's performance:
    the file's side leaves placeholders out too."""
    work = seed.work(conn)
    file_row(conn, work, 1, credit=[(VARIOUS, "Various Artists", " / "), (ONE, "Blondie", None)])
    file_row(conn, work, 2, duration_ms=300_000, credit=[(VARIOUS, "Various Artists", None)])
    file_row(conn, work, 3, duration_ms=301_000)
    file_row(
        conn, work, 4, duration_ms=201_000, credit=[(ONE, "Blondie", " / "), (VARIOUS, "VA", None)]
    )

    run(conn)

    assert credits_of(conn, fid(1)) == [(0, None, ONE, "Blondie", None, "main")]
    assert groups(conn, work) == [[fid(1), fid(4)], [fid(2), fid(3)]]
    assert credits_of(conn, fid(3)) == []


def test_untagged_file(conn: Conn) -> None:
    """E1: no recording MBID and no credit: the performance with the same version and a close
    length, else a new one."""
    work = seed.work(conn)
    file_row(conn, work, 1, title="Black", duration_ms=200_000)
    file_row(conn, work, 2, title="Black", duration_ms=201_000)
    file_row(conn, work, 3, title="Black (Acoustic)", duration_ms=200_000)
    file_row(conn, work, 4, title=None, duration_ms=199_000)

    run(conn)

    assert groups(conn, work) == [[fid(1), fid(2), fid(4)], [fid(3)]]
    assert performance_row(conn, fid(3))["version_type"] == "acoustic"


# --- run once, resume, report ---------------------------------------------------------------------


def test_it_waits_for_the_credit_backfill(conn: Conn) -> None:
    conn.execute("DELETE FROM user_settings WHERE key = %s", (ARTIST_CREDITS_BACKFILLED_SETTING,))
    file_row(conn, seed.work(conn), 1, recording_mbid=RECORDING)

    result = run(conn)

    assert result["done"] is False
    assert performance_of(conn, fid(1)) is None
    assert marker(conn, PERFORMANCES_BUILT_SETTING) is None


def test_done_once(conn: Conn) -> None:
    """The marker is set when every file on a work has a performance; a later run does nothing,
    even for a file that turned up since (the writers give it one, S3)."""
    work = seed.work(conn)
    file_row(conn, work, 1, recording_mbid=RECORDING)

    first = run(conn)
    done = marker(conn, PERFORMANCES_BUILT_SETTING)
    file_row(conn, work, 2, recording_mbid=OTHER_RECORDING)
    second = run(conn)

    assert first == {**first, "done": True, "files": 1}
    assert done is not None
    assert second["files"] == 0
    assert performance_of(conn, fid(2)) is None
    assert marker(conn, PERFORMANCES_BUILT_SETTING) == done


def test_resume(conn: Conn) -> None:
    """E5: a run that stopped left some files with a performance; the next run keeps them, gives
    the rest one by the same rule, and makes no second performance for their MBID."""
    work = seed.work(conn)
    file_row(conn, work, 1, recording_mbid=RECORDING, credit=[(ONE, "A", None)])
    file_row(conn, work, 2, recording_mbid=RECORDING)
    file_row(conn, work, 3, duration_ms=200_000)
    row = conn.execute(
        """INSERT INTO performances (work_id, recording_mbid, version_type, length_ms)
           VALUES (%s, %s, 'original', 200000) RETURNING id""",
        (work, RECORDING),
    ).fetchone()
    assert row is not None
    done = UUID(str(row["id"]))
    conn.execute(
        """INSERT INTO performance_credits (performance_id, position, mbid, credited_name, role)
           VALUES (%s, 0, %s, 'A', 'main')""",
        (done, ONE),
    )
    conn.execute("UPDATE library_files SET performance_id = %s WHERE id = %s", (done, fid(1)))

    result = run(conn)

    assert result["files"] == 2
    assert performance_of(conn, fid(1)) == done
    assert performance_of(conn, fid(2)) == done
    assert groups(conn, work) == [[fid(1), fid(2)], [fid(3)]]
    assert count(conn, "performance_credits") == 1
    assert marker(conn, PERFORMANCES_BUILT_SETTING) is not None


def test_batches_commit_as_they_go(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """Five files in batches of two: every file is reached."""
    from backend.tasks import performance_build_tasks

    monkeypatch.setattr(performance_build_tasks, "BATCH_SIZE", 2)
    work = seed.work(conn)
    for n in range(1, 6):
        file_row(conn, work, n, recording_mbid=RECORDING)

    result = run(conn)

    assert result["files"] == 5
    assert groups(conn, work) == [[fid(n) for n in range(1, 6)]]


def test_the_run_reports_on_its_own_progress_type(conn: Conn) -> None:
    file_row(conn, seed.work(conn), 1, recording_mbid=RECORDING)

    run(conn)

    row = conn.execute(
        "SELECT status FROM progress_tracking WHERE task_type = 'performances_build'"
    ).fetchone()
    assert row is not None and row["status"] == "completed"
