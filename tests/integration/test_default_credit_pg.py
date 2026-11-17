"""D1's credit for new performances, and the key by credit artists, on PostgreSQL (work merges
phase 3b PR 2, SPEC D1, D17 (scope Q4, Q5); design §6.2; AUD-R078).

``assign_performance`` (3a's S2, kept by every writer) now:

- credits a new performance whose first file has no credit (untagged, or only placeholders) with
  its work's artist (``works.artist_id``): position 0, the artist's id, its MBID if it has one,
  its name as the credited name, ``main``. A placeholder work artist gives no credit (Q5);
- compares credit artists, not MBIDs only: a credit counts as its MBID when known (the
  credit's, else its artist's), else as its artist id (Q4). An untagged file counts as its work's
  artist, so it joins a tagged performance of that artist when version and length agree
  (§6.2), and two name-only acts with the same title and length stay apart;
- reads the artist's MBID as it is now, so an artist promoted in place (2a) keeps every key.

A file that moves (a merge, a reassign, a re-scan) and has no credit of its own keeps the act of
the performance it leaves, not its new work's artist: a merge joins works of different acts,
and D1's point is that their untagged copies stay apart (lock request question A). A re-scan of
an untagged file with unchanged tags moves nothing (3a's D6).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from backend.services.performance_service import assign_performance, reassign_retagged
from backend.services.repository_factory import RepositoryFactory, work_curation_repos
from backend.services.song_match_service import SongMatchService
from backend.services.work_curation_service import merge_works, reassign_file

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]
type Credit = list[tuple[str, str | None, str | None]]
"""(mbid, name, join) entries of one file's ``credit``."""

BON_JOVI_MBID = "0a000000-0000-4000-8000-0000000000b1"
NETTLES_MBID = "0a000000-0000-4000-8000-0000000000b2"
PROMOTED_MBID = "0a000000-0000-4000-8000-0000000000b3"
VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"  # MusicBrainz's Various Artists


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


# --- seeds and reads ------------------------------------------------------------------------------


def fid(n: int) -> UUID:
    """File ids that sort as their numbers do."""
    return UUID(int=n)


def artist(conn: Conn, name: str, mbid: str | None = None) -> str:
    artist_id = f"artist-{name.lower().replace(' ', '-')}-{mbid or 'name-only'}"
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, mbid) VALUES (%s, %s, %s, %s)",
        (artist_id, name, name, mbid),
    )
    return artist_id


def work(conn: Conn, artist_id: str, title: str = "Poison") -> str:
    work_id = f"work-{title.lower()}-{artist_id}"
    conn.execute(
        "INSERT INTO works (id, title, artist_id) VALUES (%s, %s, %s)", (work_id, title, artist_id)
    )
    return work_id


def file_row(
    conn: Conn,
    work_id: str,
    n: int,
    *,
    credit: Credit | None = None,
    title: str = "Poison",
    duration_ms: int = 200_000,
) -> UUID:
    """File number *n* on *work_id*: no recording MBID, so it joins by credit, version and
    length."""
    entries = None if credit is None else [{"mbid": m, "name": a, "join": j} for m, a, j in credit]
    conn.execute(
        """INSERT INTO library_files
               (id, file_path, file_status, work_id, track_title, duration_ms, artist_mbids,
                credit)
           VALUES (%s, %s, 'present', %s, %s, %s, %s, %s)""",
        (
            fid(n),
            f"D:/music/{n}.flac",
            work_id,
            title,
            duration_ms,
            None if credit is None else [m for m, _, _ in credit],
            None if entries is None else Jsonb(entries),
        ),
    )
    return fid(n)


def assign(conn: Conn, *file_ids: UUID) -> None:
    for file_id in file_ids:
        assign_performance(file_id, RepositoryFactory(conn).performances)


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


def performance_of(conn: Conn, file_id: UUID) -> UUID | None:
    row = conn.execute(
        "SELECT performance_id FROM library_files WHERE id = %s", (file_id,)
    ).fetchone()
    assert row is not None
    return None if row["performance_id"] is None else UUID(str(row["performance_id"]))


# --- D1's credit for a new performance ------------------------------------------------------------


def test_a_new_untagged_performance_credits_its_works_artist(conn: Conn) -> None:
    bon_jovi = artist(conn, "Bon Jovi", BON_JOVI_MBID)
    untagged = file_row(conn, work(conn, bon_jovi), 1)

    assign(conn, untagged)

    assert credits_of(conn, untagged) == [(0, bon_jovi, BON_JOVI_MBID, "Bon Jovi", None, "main")]


def test_a_name_only_works_artist_is_credited_by_id_alone(conn: Conn) -> None:
    poison = artist(conn, "Poison")
    untagged = file_row(conn, work(conn, poison), 1)

    assign(conn, untagged)

    assert credits_of(conn, untagged) == [(0, poison, None, "Poison", None, "main")]


def test_placeholders_only_take_the_works_artist(conn: Conn) -> None:
    poison = artist(conn, "Poison")
    only_placeholder = file_row(
        conn, work(conn, poison), 1, credit=[(VARIOUS, "Various Artists", None)]
    )

    assign(conn, only_placeholder)

    assert credits_of(conn, only_placeholder) == [(0, poison, None, "Poison", None, "main")]


def test_a_placeholder_work_artist_gives_no_credit(conn: Conn) -> None:
    various = artist(conn, "Various Artists", VARIOUS)
    untagged = file_row(conn, work(conn, various), 1)

    assign(conn, untagged)

    assert performance_of(conn, untagged) is not None
    assert credits_of(conn, untagged) == []


def test_a_tagged_file_is_credited_from_its_tags(conn: Conn) -> None:
    """A tag credit wins over the work's artist, as in 3a."""
    poison = artist(conn, "Poison")
    tagged = file_row(
        conn, work(conn, poison), 1, credit=[(NETTLES_MBID, "Jennifer Nettles", None)]
    )

    assign(conn, tagged)

    assert credits_of(conn, tagged) == [(0, None, NETTLES_MBID, "Jennifer Nettles", None, "main")]


# --- the key by credit artists (Q4) ---------------------------------------------------------------


def test_an_untagged_copy_joins_its_artists_tagged_performance(conn: Conn) -> None:
    """§6.2: the same credit artists (Bon Jovi's MBID), version and a length within 3 s. A copy
    of another version, or 3,001 ms off, starts its own."""
    bon_jovi = artist(conn, "Bon Jovi", BON_JOVI_MBID)
    song = work(conn, bon_jovi)
    tagged = file_row(conn, song, 1, credit=[(BON_JOVI_MBID, "Bon Jovi", None)])
    near = file_row(conn, song, 2, duration_ms=202_000)
    live = file_row(conn, song, 3, title="Poison (Live)")
    far = file_row(conn, song, 4, duration_ms=196_999)

    assign(conn, tagged, near, live, far)

    assert groups(conn, song) == [[tagged, near], [live], [far]]


def test_promotion_in_place_keeps_the_key(conn: Conn) -> None:
    """A name-only artist credited by id gains an MBID (2a promotes it in place). Its untagged
    copies, and its credit, then both count as that MBID, so a later copy still joins."""
    poison = artist(conn, "Poison")
    song = work(conn, poison)
    first = file_row(conn, song, 1)
    assign(conn, first)
    conn.execute("UPDATE artists SET mbid = %s WHERE id = %s", (PROMOTED_MBID, poison))
    later = file_row(conn, song, 2, duration_ms=201_000)
    tagged = file_row(conn, song, 3, duration_ms=199_000, credit=[(PROMOTED_MBID, "Poison", None)])

    assign(conn, later, tagged)

    assert groups(conn, song) == [[first, later, tagged]]


# --- moving files keep their act (question A) -----------------------------------------------------


def test_two_name_only_acts_stay_apart_after_a_merge(conn: Conn) -> None:
    """Two works of "Poison" by two name-only acts, each with an untagged copy of the same
    length. Merged, each copy keeps its own performance and credit (3a's empty key joined
    them)."""
    cooper, bbd = artist(conn, "Alice Cooper"), artist(conn, "Bell Biv DeVoe")
    target, source = work(conn, cooper), work(conn, bbd)
    ours = file_row(conn, target, 1)
    theirs = file_row(conn, source, 2)
    assign(conn, ours, theirs)

    merge_works(target, [source], work_curation_repos(RepositoryFactory(conn)))

    assert groups(conn, target) == [[ours], [theirs]]
    assert credits_of(conn, ours) == [(0, cooper, None, "Alice Cooper", None, "main")]
    assert credits_of(conn, theirs) == [(0, bbd, None, "Bell Biv DeVoe", None, "main")]


def test_a_merge_keeps_a_local_artist_a_performance_credits(conn: Conn) -> None:
    """The merge's local-artist delete (AUD-R035, 2a) also keeps an artist a performance credits:
    the source's act left with no work still performs a copy of the merged work (question B)."""
    cooper, bbd = artist(conn, "Alice Cooper"), artist(conn, "Bell Biv DeVoe")
    target, source = work(conn, cooper), work(conn, bbd)
    assign(conn, file_row(conn, target, 1), file_row(conn, source, 2))

    merge_works(target, [source], work_curation_repos(RepositoryFactory(conn)))

    kept = conn.execute("SELECT id FROM artists WHERE id = %s", (bbd,)).fetchone()
    assert kept is not None


def test_a_moved_untagged_file_keeps_its_act(conn: Conn) -> None:
    """Reassigned to a work of another artist, an untagged copy keeps the act of the
    performance it left: it joins no performance of the new work's artist (question A)."""
    cooper, bbd = artist(conn, "Alice Cooper"), artist(conn, "Bell Biv DeVoe")
    home, elsewhere = work(conn, cooper), work(conn, bbd)
    moving = file_row(conn, home, 1)
    staying = file_row(conn, elsewhere, 2)
    assign(conn, moving, staying)

    reassign_file(moving, elsewhere, work_curation_repos(RepositoryFactory(conn)))

    assert groups(conn, elsewhere) == [[moving], [staying]]
    assert credits_of(conn, moving) == [(0, cooper, None, "Alice Cooper", None, "main")]


def test_a_rescan_moves_no_untagged_file_after_a_merge(conn: Conn) -> None:
    """3a's D6: only a tag change moves a file. The source's untagged copy, now on the target,
    still fits the performance it holds."""
    cooper, bbd = artist(conn, "Alice Cooper"), artist(conn, "Bell Biv DeVoe")
    target, source = work(conn, cooper), work(conn, bbd)
    ours = file_row(conn, target, 1)
    theirs = file_row(conn, source, 2)
    assign(conn, ours, theirs)
    repos = RepositoryFactory(conn)
    merge_works(target, [source], work_curation_repos(repos))
    held = performance_of(conn, theirs)

    moved = reassign_retagged(
        ["D:/music/1.flac", "D:/music/2.flac"], repos.performances, SongMatchService(repos.matches)
    )

    assert moved == 0
    assert performance_of(conn, theirs) == held
