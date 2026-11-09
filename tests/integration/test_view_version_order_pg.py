"""Air play with version entries: ``play_file_resolution`` (versions phase 2b, SPEC S4, E4;
design C61; AUD-R033's 2b check).

A matched line plays the first of these that has an entry for the line's work:

1. the version entry for (work, the line's version text) -- ``source`` ``version``;
2. the format override for the station's format -- ``override``;
3. the song master -- ``master``.

A line with no version text, or with one nothing maps, plays as before. An entry for another
text, or on another work, is never used. The entry is read for the line's work as the view
finds it: the matched file's work, else its recording's work. The step that wins is reported
even when its file is unavailable (the locked ``test_play_file_resolution.py``; E1's missing
file is in ``test_version_entry_upkeep_pg.py``).

AUD-R033: the view only looks up keys. It joins ``format_overrides`` a second time by (work,
version text), with no new subquery and no title parsing, and keeps its columns.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn, at

pytestmark = pytest.mark.integration

type Plays = tuple[UUID | None, str | None]
"""(file_id, source) of one play."""


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def file_on(conn: Conn, work_id: str | None) -> UUID:
    """A file whose own ``work_id`` is *work_id*."""
    file_id = seed.library_file(conn)
    conn.execute("UPDATE library_files SET work_id = %s WHERE id = %s", (work_id, file_id))
    return file_id


def version_entry(conn: Conn, work_id: str, text: str, file_id: UUID) -> None:
    conn.execute(
        """INSERT INTO format_overrides (work_id, version_text, preferred_file_id)
           VALUES (%s, %s, %s)""",
        (work_id, text, file_id),
    )


def line(conn: Conn, title: str, matched: UUID, *, format_name: str | None = "AC") -> UUID:
    """One play, at a fresh station of *format_name*, of a fresh line titled *title* whose
    one match is *matched*."""
    station = seed.station(conn, format_name=format_name)
    return seed.matched_play(conn, seed.playlist(conn, station), at("08:00"), matched, title=title)


def plays(conn: Conn, event: UUID) -> Plays:
    [row] = conn.execute(
        "SELECT file_id, source FROM play_file_resolution WHERE play_event_id = %s", (event,)
    ).fetchall()
    return (None if row["file_id"] is None else UUID(str(row["file_id"])), row["source"])


class World:
    """A work with a master, a live copy and an AC format override's copy."""

    def __init__(self, conn: Conn) -> None:
        self.work = seed.work(conn)
        self.master = file_on(conn, self.work)
        self.live = file_on(conn, self.work)
        self.ac = file_on(conn, self.work)
        seed.song_master(conn, self.work, self.master)


@pytest.fixture
def world(conn: Conn) -> World:
    return World(conn)


def test_a_mapped_version_plays_its_file(conn: Conn, world: World) -> None:
    version_entry(conn, world.work, "live", world.live)

    event = line(conn, "Enter Sandman (Live)", world.master, format_name=None)

    assert plays(conn, event) == (world.live, "version")


def test_version_beats_format(conn: Conn, world: World) -> None:
    """E4: a version line at a station whose format has an override plays the version."""
    version_entry(conn, world.work, "live", world.live)
    seed.format_override(conn, world.work, "AC", world.ac)

    version_line = line(conn, "Enter Sandman (Live)", world.master, format_name="AC")
    plain_line = line(conn, "Enter Sandman", world.master, format_name="AC")

    assert plays(conn, version_line) == (world.live, "version")
    assert plays(conn, plain_line) == (world.ac, "override")


def test_an_unmapped_version_plays_as_if_it_had_none(conn: Conn, world: World) -> None:
    seed.format_override(conn, world.work, "AC", world.ac)
    version_entry(conn, world.work, "live", world.live)

    at_ac = line(conn, "Enter Sandman (Acoustic)", world.master, format_name="AC")
    at_rock = line(conn, "Enter Sandman (Acoustic)", world.master, format_name="Rock")

    assert plays(conn, at_ac) == (world.ac, "override")
    assert plays(conn, at_rock) == (world.master, "master")


def test_a_line_without_version_text_never_plays_an_entry(conn: Conn, world: World) -> None:
    version_entry(conn, world.work, "live", world.live)

    plain = line(conn, "Enter Sandman", world.master, format_name=None)
    dashed = line(conn, "Enter Sandman - Live", world.master, format_name=None)

    assert plays(conn, plain) == (world.master, "master")
    assert plays(conn, dashed) == (world.master, "master")


def test_only_the_lines_own_text_and_work(conn: Conn, world: World) -> None:
    """ "live" doesn't answer "live s&m", and another work's "live s&m" doesn't either."""
    other = seed.work(conn)
    version_entry(conn, world.work, "live", world.live)
    version_entry(conn, other, "live s&m", file_on(conn, other))

    event = line(conn, "Enter Sandman (Live S&M)", world.master, format_name=None)

    assert plays(conn, event) == (world.master, "master")


def test_texts_mapped_to_one_file(conn: Conn, world: World) -> None:
    """Design 6.8: "(Live)" and "(Lv)" can share one file."""
    version_entry(conn, world.work, "live", world.live)
    version_entry(conn, world.work, "lv", world.live)

    live = line(conn, "Enter Sandman (Live)", world.master, format_name=None)
    lv = line(conn, "Enter Sandman (Lv)", world.master, format_name=None)

    assert plays(conn, live) == plays(conn, lv) == (world.live, "version")


def test_the_entry_of_the_recordings_work(conn: Conn, world: World) -> None:
    """A matched file with no work of its own reaches the work through its recording."""
    version_entry(conn, world.work, "live", world.live)
    on_recording = seed.library_file(conn, recording_id=seed.recording(conn, world.work))

    event = line(conn, "Enter Sandman (Live)", on_recording, format_name=None)

    assert plays(conn, event) == (world.live, "version")


def test_the_files_own_work_before_its_recordings(conn: Conn, world: World) -> None:
    """A matched file with a work of its own and a recording on another reads its own work's
    entry, as the view finds its work."""
    other = seed.work(conn)
    version_entry(conn, world.work, "live", world.live)
    version_entry(conn, other, "live", file_on(conn, other))
    two_works = seed.library_file(conn, recording_id=seed.recording(conn, other))
    conn.execute("UPDATE library_files SET work_id = %s WHERE id = %s", (world.work, two_works))

    event = line(conn, "Enter Sandman (Live)", two_works, format_name=None)

    assert plays(conn, event) == (world.live, "version")


def test_a_version_entry_with_no_master_plays(conn: Conn) -> None:
    """The version step needs no master: a work with only an entry plays it for its line, and
    nothing for a plain line."""
    work = seed.work(conn)
    matched, live = file_on(conn, work), file_on(conn, work)
    version_entry(conn, work, "live", live)

    version_line = line(conn, "Black (Live)", matched, format_name=None)
    plain_line = line(conn, "Black", matched, format_name=None)

    assert plays(conn, version_line) == (live, "version")
    assert plays(conn, plain_line) == (None, None)


def test_an_unmatched_line_plays_nothing(conn: Conn, world: World) -> None:
    version_entry(conn, world.work, "live", world.live)
    review = seed.identity(conn, title="Enter Sandman (Live)", status="needs_review")
    seed.match(conn, review, world.master)
    event = seed.play(conn, seed.playlist(conn, seed.station(conn)), review, at("08:00"))

    assert plays(conn, event) == (None, None)


def test_the_view_only_looks_up_keys(conn: Conn) -> None:
    """AUD-R033: ``format_overrides`` joined twice, by key; the same one subquery (the best
    match) as before; no title parsing; the same columns in the same order."""
    row = conn.execute("SELECT pg_get_viewdef('play_file_resolution') AS d").fetchone()
    assert row is not None
    definition = " ".join(row["d"].lower().split())
    columns = conn.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_name = 'play_file_resolution' ORDER BY ordinal_position"""
    ).fetchall()

    assert definition.count("join format_overrides") == 2
    assert re.search(r"\.version_text = \w+\.version_text", definition)
    assert definition.count("select") == 2
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
