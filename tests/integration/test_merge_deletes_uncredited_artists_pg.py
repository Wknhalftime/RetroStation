"""The merge's local-artist delete by credits (work merges phase 3b PR 5b, SPEC S7, D19; scope
note R5; AUD-R004 as changed by AUD-R035; AUD-R088), on PostgreSQL.

With ``works.artist_id`` gone, a merge's candidates are the artists credited by the source
performances it deletes: a performance its files leave for another (the same recording MBID)
goes with its credits. A candidate is deleted when it is local, no performance credits it any
more, no log-name link names it and it is on neither side of an artist merge. The "no work"
condition is gone (D19); an artist the merge never touched is never a candidate.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.services.repository_factory import RepositoryFactory, work_curation_repos
from backend.services.work_curation_service import merge_works

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

SHARED = "0b000000-0000-4000-8000-0000000000f1"  # one recording MBID under both works
OWN = "0b000000-0000-4000-8000-0000000000f2"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def artist(conn: Conn, name: str, mbid: str | None = None) -> str:
    artist_id = f"artist-{name.lower().replace(' ', '-')}"
    conn.execute(
        """INSERT INTO artists (id, name, sort_name, normalized_name, mbid, origin)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (artist_id, name, name, name.lower(), mbid, "musicbrainz" if mbid else "local"),
    )
    return artist_id


def work(conn: Conn, title: str = "Poison") -> str:
    work_id = f"work-{uuid4()}"
    conn.execute("INSERT INTO works (id, title) VALUES (%s, %s)", (work_id, title))
    return work_id


def performance_with_file(conn: Conn, work_id: str, artist_id: str, recording_mbid: str) -> UUID:
    """A performance of *work_id* by *artist_id* holding one present file of *recording_mbid*."""
    row = conn.execute(
        """INSERT INTO performances (work_id, recording_mbid, version_type, length_ms)
           VALUES (%s, %s, 'original', 200000) RETURNING id""",
        (work_id, recording_mbid),
    ).fetchone()
    assert row is not None
    performance_id = UUID(str(row["id"]))
    conn.execute(
        """INSERT INTO performance_credits (performance_id, position, artist_id, role)
           VALUES (%s, 0, %s, 'main')""",
        (performance_id, artist_id),
    )
    file_id = uuid4()
    conn.execute(
        """INSERT INTO library_files (id, file_path, file_status, work_id, performance_id,
                                      recording_mbid, track_title, duration_ms)
           VALUES (%s, %s, 'present', %s, %s, %s, 'Poison', 200000)""",
        (file_id, f"D:/music/{file_id}.flac", work_id, performance_id, recording_mbid),
    )
    conn.execute(
        "UPDATE performances SET preferred_copy_id = %s WHERE id = %s", (file_id, performance_id)
    )
    return performance_id


def merge(conn: Conn, target: str, source: str) -> None:
    merge_works(target, [source], work_curation_repos(RepositoryFactory(conn)))


def kept(conn: Conn, artist_id: str) -> bool:
    return conn.execute("SELECT 1 FROM artists WHERE id = %s", (artist_id,)).fetchone() is not None


def two_works_of_one_recording(conn: Conn, target_act: str, source_act: str) -> tuple[str, str]:
    """Target and source each perform the shared recording: merged, the source's file joins the
    target's performance and the source's emptied performance goes with its credit."""
    target, source = work(conn), work(conn)
    performance_with_file(conn, target, target_act, SHARED)
    performance_with_file(conn, source, source_act, SHARED)
    return target, source


def test_a_local_artist_credited_only_by_a_deleted_performance_goes(conn: Conn) -> None:
    cooper, bbd = artist(conn, "Alice Cooper"), artist(conn, "Bell Biv DeVoe")
    target, source = two_works_of_one_recording(conn, cooper, bbd)

    merge(conn, target, source)

    assert not kept(conn, bbd)
    assert kept(conn, cooper)


def test_an_artist_another_performance_credits_stays(conn: Conn) -> None:
    cooper, bbd = artist(conn, "Alice Cooper"), artist(conn, "Bell Biv DeVoe")
    target, source = two_works_of_one_recording(conn, cooper, bbd)
    elsewhere = work(conn, "Do Me!")
    performance_with_file(conn, elsewhere, bbd, OWN)

    merge(conn, target, source)

    assert kept(conn, bbd)


def test_a_musicbrainz_artist_stays(conn: Conn) -> None:
    cooper = artist(conn, "Alice Cooper")
    bbd = artist(conn, "Bell Biv DeVoe", "0a000000-0000-4000-8000-0000000000f3")
    target, source = two_works_of_one_recording(conn, cooper, bbd)

    merge(conn, target, source)

    assert kept(conn, bbd)


@pytest.mark.parametrize("side", ["member", "primary"])
def test_an_artist_in_an_artist_merge_stays(conn: Conn, side: str) -> None:
    cooper, bbd = artist(conn, "Alice Cooper"), artist(conn, "Bell Biv DeVoe")
    other = artist(conn, "BBD")
    member, primary = (bbd, other) if side == "member" else (other, bbd)
    conn.execute(
        "INSERT INTO artist_merges (artist_id, primary_id) VALUES (%s, %s)", (member, primary)
    )
    target, source = two_works_of_one_recording(conn, cooper, bbd)

    merge(conn, target, source)

    assert kept(conn, bbd)


def test_an_artist_a_link_names_stays(conn: Conn) -> None:
    cooper, bbd = artist(conn, "Alice Cooper"), artist(conn, "Bell Biv DeVoe")
    name = conn.execute(
        """INSERT INTO broadcast_artists (original_name, normalized_name)
           VALUES ('BELL BIV DEVOE', 'bell biv devoe') RETURNING id"""
    ).fetchone()
    assert name is not None
    conn.execute(
        """INSERT INTO log_name_links (broadcast_artist_id, artist_id, state, origin, strength,
                                       evidence)
           VALUES (%s, %s, 'active', 'manual', 100, '{"version": 1}')""",
        (name["id"], bbd),
    )
    target, source = two_works_of_one_recording(conn, cooper, bbd)

    merge(conn, target, source)

    assert kept(conn, bbd)


def test_an_artist_the_merge_never_touched_is_no_candidate(conn: Conn) -> None:
    """A local artist credited by nothing before the merge is not the merge's to delete."""
    cooper, bbd = artist(conn, "Alice Cooper"), artist(conn, "Bell Biv DeVoe")
    bystander = artist(conn, "Warrant")
    target, source = two_works_of_one_recording(conn, cooper, bbd)

    merge(conn, target, source)

    assert kept(conn, bystander)
