"""The work merge (artist model phase 3b, SPEC S3 and E1; design C58, AUD-R032).

PR 1 is the merge's master rule, a declared change. Until 3b a merge dropped every source's
master and re-picked the target's: a valid hand-picked master on the target stayed, and
anything else gave way to the best present file, stored automatic. Now:

- a hand-picked master is a candidate while its file is a present file of the merged work. One
  whose file is missing is no candidate, and the recalculation replaces it as before;
- a hand-picked master beats an automatic one (or none), whichever work holds it, and stays
  hand-picked on the merged work;
- between hand-picked masters, the work with more matched plays keeps its master. A work's
  matched plays are the plays of the lines whose view work (``play_file_resolution.work_id``)
  is that work, counted before anything moves, on the merge's own connection. A tie goes to
  the target, and between sources to the one listed first;
- with no hand-picked master left, the master is re-picked from every present file of the
  merged work and stored automatic, as before. Plays keep no automatic master: the next
  matching run on the work would re-pick it (lock request, question A).

Every seed stays uncommitted, so a count made on another connection would see no plays.
``merge_works`` is called directly (D3: PR 2's guard wraps it). The tests read only the merged
work's master and the lines of the work that keeps its master, which play the same file
before and after. PR 2 pins the other work's lines, and its cases go beside these.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.services import work_curation_service as curation
from backend.services.repository_factory import RepositoryFactory, work_curation_repos
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


# --- seeds and reads ------------------------------------------------------------------------------


def copy(conn: Conn, work_id: str, fmt: str = "mp3", *, status: str = "present") -> UUID:
    """A file of *work_id* in *fmt*: by the master rules, flac scores above mp3."""
    file_id = seed.library_file(conn, status=status)
    conn.execute(
        "UPDATE library_files SET work_id = %s, format = %s WHERE id = %s",
        (work_id, fmt, file_id),
    )
    return file_id


def master(conn: Conn, work_id: str, file_id: UUID, method: str) -> None:
    conn.execute(
        """INSERT INTO song_masters (work_id, preferred_file_id, selection_method)
           VALUES (%s, %s, %s)""",
        (work_id, file_id, method),
    )


def mastered(conn: Conn, method: str, fmt: str = "mp3") -> tuple[str, UUID]:
    """A work whose one file is its master, picked by *method* (``manual`` or ``auto``)."""
    work = seed.work(conn)
    file_id = copy(conn, work, fmt)
    master(conn, work, file_id, method)
    return work, file_id


def played(conn: Conn, identity_id: UUID, times: int) -> list[UUID]:
    """*times* plays of the line at a station with no format, so no format override reaches
    it; the plays."""
    playlist = seed.playlist(conn, seed.station(conn, format_name=None))
    return [
        seed.play(conn, playlist, identity_id, seed.at(f"{8 + n:02d}:00")) for n in range(times)
    ]


def line(conn: Conn, file_id: UUID, times: int, *, status: str = "auto_matched") -> list[UUID]:
    """A log line whose one match is *file_id*, played *times* times; the plays."""
    identity_id = seed.identity(conn, title="AGAIN", artist="JANET", status=status)
    seed.match(conn, identity_id, file_id)
    return played(conn, identity_id, times)


def merge(conn: Conn, target: str, sources: list[str]) -> None:
    curation.merge_works(target, sources, work_curation_repos(RepositoryFactory(conn)))


def master_of(conn: Conn, work_id: str) -> tuple[UUID, str] | None:
    """(file, method) of the work's master."""
    row = conn.execute(
        "SELECT preferred_file_id, selection_method FROM song_masters WHERE work_id = %s",
        (work_id,),
    ).fetchone()
    return None if row is None else (UUID(str(row["preferred_file_id"])), row["selection_method"])


def plays(conn: Conn, events: list[UUID]) -> set[UUID | None]:
    """The files *events* play, read only through the view."""
    rows = conn.execute(
        "SELECT file_id FROM play_file_resolution WHERE play_event_id = ANY(%s)", (events,)
    ).fetchall()
    assert len(rows) == len(events)
    return {None if r["file_id"] is None else UUID(str(r["file_id"])) for r in rows}


# --- a hand-picked master beats an automatic one -------------------------------------------------


@pytest.mark.parametrize("hand_picked", ["target", "source"])
def test_a_hand_picked_master_beats_an_automatic_one(conn: Conn, hand_picked: str) -> None:
    """Whichever work holds it, though the automatic side has more plays and the better file:
    the hand-picked master stays hand-picked, and its lines keep playing it."""
    picked_work, picked = mastered(conn, "manual")
    auto_work, auto_master = mastered(conn, "auto", "flac")
    picked_lines = line(conn, picked, 1)
    line(conn, auto_master, 3)
    target, source = (
        (picked_work, auto_work) if hand_picked == "target" else (auto_work, picked_work)
    )
    assert plays(conn, picked_lines) == {picked}

    merge(conn, target, [source])

    assert master_of(conn, target) == (picked, "manual")
    assert plays(conn, picked_lines) == {picked}


# --- E1: two hand-picked masters ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("target_plays", "source_plays", "keeps"),
    [(3, 1, "target"), (1, 3, "source"), (2, 2, "target")],
    ids=["the-target-has-more-plays", "the-source-has-more-plays", "a-tie-goes-to-the-target"],
)
def test_two_manual_masters(conn: Conn, target_plays: int, source_plays: int, keeps: str) -> None:
    """E1: the work with more matched plays keeps its hand-picked master, whichever file
    scores better (the source's is flac); a tie goes to the target. Its lines keep playing
    it."""
    target, target_pick = mastered(conn, "manual")
    source, source_pick = mastered(conn, "manual", "flac")
    target_lines = line(conn, target_pick, target_plays)
    source_lines = line(conn, source_pick, source_plays)
    kept, kept_lines = (
        (target_pick, target_lines) if keeps == "target" else (source_pick, source_lines)
    )

    merge(conn, target, [source])

    assert master_of(conn, target) == (kept, "manual")
    assert plays(conn, kept_lines) == {kept}


def test_more_plays_not_more_lines(conn: Conn) -> None:
    """Plays are counted, not lines: one source line played 3 times beats two target lines
    played once each (lock request, question B)."""
    target, target_pick = mastered(conn, "manual")
    source, source_pick = mastered(conn, "manual")
    line(conn, target_pick, 1)
    line(conn, target_pick, 1)
    source_lines = line(conn, source_pick, 3)

    merge(conn, target, [source])

    assert master_of(conn, target) == (source_pick, "manual")
    assert plays(conn, source_lines) == {source_pick}


def test_only_lines_whose_view_work_is_the_work_count(conn: Conn) -> None:
    """A work's plays are those of the lines air play gives that work. A line still in review
    plays nothing, and a line whose best match is a file of a third work belongs to that work,
    though both also match the target's file. So the source's one manually matched play beats
    the target's six."""
    target, target_pick = mastered(conn, "manual")
    source, source_pick = mastered(conn, "manual")
    line(conn, target_pick, 3, status="needs_review")
    elsewhere = seed.identity(conn, title="AGAIN", artist="JANET")
    seed.match(conn, elsewhere, copy(conn, seed.work(conn)), confidence=0.95)
    seed.match(conn, elsewhere, target_pick, confidence=0.5)
    played(conn, elsewhere, 3)
    source_lines = line(conn, source_pick, 1, status="manual_matched")

    merge(conn, target, [source])

    assert master_of(conn, target) == (source_pick, "manual")
    assert plays(conn, source_lines) == {source_pick}


# --- no hand-picked master: re-picked as before -------------------------------------------------


@pytest.mark.parametrize(
    ("target_format", "target_plays", "source_format", "source_plays", "keeps"),
    [("mp3", 3, "flac", 1, "source"), ("flac", 1, "mp3", 3, "target")],
    ids=["the-target-has-more-plays", "the-source-has-more-plays"],
)
def test_two_automatic_masters_are_re_picked_from_every_file(
    conn: Conn,
    target_format: str,
    target_plays: int,
    source_format: str,
    source_plays: int,
    keeps: str,
) -> None:
    """Question A: with no hand-picked master, plays keep nothing. The master is re-picked
    from every present file of the merged work and stored automatic, as before 3b: here the
    better file is the master of the work with fewer plays."""
    target, target_master = mastered(conn, "auto", target_format)
    source, source_master = mastered(conn, "auto", source_format)
    line(conn, target_master, target_plays)
    line(conn, source_master, source_plays)
    best = target_master if keeps == "target" else source_master

    merge(conn, target, [source])

    assert master_of(conn, target) == (best, "auto")


# --- an invalid hand-picked master ----------------------------------------------------------------


def test_a_valid_hand_picked_master_beats_an_invalid_one(conn: Conn) -> None:
    """A hand-picked master whose file is missing is no candidate, though its work has more
    plays and a better present file: the other work's valid hand-pick is kept (question C)."""
    target = seed.work(conn)
    gone = copy(conn, target, status="missing")
    better = copy(conn, target, "flac")
    master(conn, target, gone, "manual")
    line(conn, better, 3)
    source, source_pick = mastered(conn, "manual")
    source_lines = line(conn, source_pick, 1)

    merge(conn, target, [source])

    assert master_of(conn, target) == (source_pick, "manual")
    assert plays(conn, source_lines) == {source_pick}


@pytest.mark.parametrize("invalid_on", ["target", "source"])
def test_an_invalid_hand_picked_master_is_replaced_as_before(conn: Conn, invalid_on: str) -> None:
    """With no valid hand-pick left, the recalculation's rule for an invalid manual pick is
    unchanged: the best present file of the merged work becomes the master, stored automatic,
    whatever the plays (the 0c test of the recalculation pins the same rule for reassign)."""
    invalid = seed.work(conn)
    gone = copy(conn, invalid, "flac", status="missing")
    best = copy(conn, invalid, "flac")
    master(conn, invalid, gone, "manual")
    line(conn, best, 1)
    auto_work, auto_master = mastered(conn, "auto")
    line(conn, auto_master, 3)
    target, source = (invalid, auto_work) if invalid_on == "target" else (auto_work, invalid)

    merge(conn, target, [source])

    assert master_of(conn, target) == (best, "auto")


# --- several works ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a_plays", "b_plays", "keeps"),
    [(2, 1, "a"), (2, 2, "b")],
    ids=["more-plays-beats-the-listing", "a-tie-between-sources-goes-to-the-one-listed-first"],
)
def test_several_works(conn: Conn, a_plays: int, b_plays: int, keeps: str) -> None:
    """Three works, the sources listed b then a. The target's automatic master loses to the
    sources' hand-picked ones, though it has the most plays and the best file; between those,
    more plays wins, then the source listed first."""
    target, target_master = mastered(conn, "auto", "flac")
    line(conn, target_master, 5)
    a, a_pick = mastered(conn, "manual")
    b, b_pick = mastered(conn, "manual")
    line(conn, a_pick, a_plays)
    line(conn, b_pick, b_plays)
    kept = a_pick if keeps == "a" else b_pick

    merge(conn, target, [b, a])

    assert master_of(conn, target) == (kept, "manual")
