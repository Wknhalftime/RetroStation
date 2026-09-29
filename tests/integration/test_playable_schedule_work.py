"""Acceptance tests: what the schedule reader does with a resolved file (D16-D19, D21, D22).

Which file plays is curation's rule, tested at the view ``play_file_resolution``; which plays
make the day is broadcast's, tested at ``station_day_plays``. Here only the reader's side:

- it plays what the view resolves, indexed by the station-day ``position`` (D19);
- a final file that is not present (D21) gives ``file=None`` plus a
  ``schedule_file_unavailable`` warning, with no fallback (D16);
- a file row with a negative ``duration_ms`` gives ``file=None`` plus a
  ``schedule_file_invalid`` warning, without disturbing the rest of the day (D18);
- a play logs at most one warning: an unavailable or invalid file's cues are not read.

Under D22 a matched file is the final file only as its work's master, so "the matched file
is the bad one" is seeded with ``seed.mastered_file``.

DRAFT for D22: replaces ``test_playable_schedule_work.py`` once the user approves.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping, Sequence
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row
from structlog.testing import capture_logs

from backend.db.repositories.playable_schedule import PgPlayableScheduleRepository
from backend.domain.streaming import PlayableFile, ScheduleItem
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import DAY, Conn, at


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def get_day(conn: Conn, station_id: UUID) -> list[ScheduleItem]:
    return PgPlayableScheduleRepository(conn).get_day(station_id, DAY)


def warnings(logs: Sequence[MutableMapping[str, Any]], event: str) -> list[Any]:
    return [e for e in logs if e["event"] == event and e["log_level"] == "warning"]


def file_on_work(conn: Conn, work_id: str | None, *, status: str = "present") -> UUID:
    """A library file, with no recording, whose own ``library_files.work_id`` is ``work_id``."""
    file_id = seed.library_file(conn, status=status, recording_id=None)
    conn.execute("UPDATE library_files SET work_id = %s WHERE id = %s", (work_id, file_id))
    return file_id


def playable(conn: Conn, file_id: UUID) -> PlayableFile:
    """The ``PlayableFile`` a seeded file with default duration and no cue row reads as."""
    return PlayableFile(
        file_id=file_id, path=seed.file_path(conn, file_id), duration_ms=200_000, cues=None
    )


def test_reader_plays_the_master_of_the_file_own_work(conn: Conn) -> None:
    """End to end: ``get_day`` plays the master reached through ``library_files.work_id``."""
    st = seed.station(conn)
    work = seed.work(conn)
    matched, master = file_on_work(conn, work), file_on_work(conn, work)
    seed.song_master(conn, work, master)
    event = seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)

    items = get_day(conn, st)

    assert [(item.event_id, item.file) for item in items] == [(event, playable(conn, master))]


@pytest.mark.parametrize("status", ["missing", "deleted"])
@pytest.mark.parametrize("source", ["master", "override"])
def test_unavailable_final_file_gives_no_file_no_fallback_and_a_warning(
    conn: Conn, source: str, status: str
) -> None:
    """Test 6 (reader): a final file that is not present is NOT replaced by the matched file."""
    st = seed.station(conn, format_name="AC")
    work = seed.work(conn)
    matched = file_on_work(conn, work)
    final = file_on_work(conn, work, status=status if source == "master" else "present")
    seed.song_master(conn, work, final)
    if source == "override":
        final = file_on_work(conn, work, status=status)
        seed.format_override(conn, work, "AC", final)
    event = seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(item.event_id, item.file) for item in items] == [(event, None)]
    [warning] = warnings(logs, "schedule_file_unavailable")
    assert str(warning["event_id"]) == str(event)
    assert str(warning["file_id"]) == str(final)
    assert str(warning["file_status"]) == status
    assert str(warning["source"]) == source


@pytest.mark.parametrize("source", ["matched-is-master", "other-master"])
def test_negative_duration_gives_no_file_and_a_warning_and_spares_the_day(
    conn: Conn, source: str
) -> None:
    """Test 7 (D18): a negative ``duration_ms`` on the final file affects only its own play."""
    st = seed.station(conn)
    pl = seed.playlist(conn, st)
    before_file, after_file = seed.mastered_file(conn), seed.mastered_file(conn)
    if source == "matched-is-master":
        matched = bad = seed.mastered_file(conn)
    else:
        work = seed.work(conn)
        matched, bad = file_on_work(conn, work), file_on_work(conn, work)
        seed.song_master(conn, work, bad)
    # The column has no CHECK, so a raw UPDATE stores the impossible value.
    conn.execute("UPDATE library_files SET duration_ms = -1 WHERE id = %s", (bad,))
    before = seed.matched_play(conn, pl, at("07:00"), before_file)
    broken = seed.matched_play(conn, pl, at("08:00"), matched)
    after = seed.matched_play(conn, pl, at("09:00"), after_file)

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(item.event_id, item.file) for item in items] == [
        (before, playable(conn, before_file)),
        (broken, None),
        (after, playable(conn, after_file)),
    ]
    [warning] = warnings(logs, "schedule_file_invalid")
    assert str(warning["event_id"]) == str(broken)
    assert str(warning["file_id"]) == str(bad)
    assert warning["error"]


def test_final_file_with_a_status_other_than_present_is_unavailable(conn: Conn) -> None:
    """D21: playable means ``present``; any other status, even an unknown one, is unavailable.

    ``library_files.file_status`` is plain TEXT with no CHECK, so a raw UPDATE stores it.
    """
    st = seed.station(conn)
    final = seed.mastered_file(conn)
    conn.execute("UPDATE library_files SET file_status = 'quarantined' WHERE id = %s", (final,))
    event = seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), final)

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(item.event_id, item.file) for item in items] == [(event, None)]
    [warning] = warnings(logs, "schedule_file_unavailable")
    assert str(warning["event_id"]) == str(event)
    assert str(warning["file_id"]) == str(final)
    assert str(warning["file_status"]) == "quarantined"
    assert str(warning["source"]) == "master"


def test_reader_index_is_the_station_day_position(conn: Conn) -> None:
    """D19: ``get_day(...)[i]`` is the ``station_day_plays`` row with ``position = i``."""
    st = seed.station(conn)
    first_pl, second_pl = seed.playlist(conn, st), seed.playlist(conn, st)
    tied = seed.identity(conn)
    seed.match(conn, tied, seed.mastered_file(conn))
    seed.play(conn, second_pl, tied, at("09:00"), event_id=UUID(int=0x2))
    seed.play(conn, first_pl, tied, at("09:00"), event_id=UUID(int=0x1))
    seed.play(conn, first_pl, seed.identity(conn, status="pending"), at("08:30"))
    seed.matched_play(conn, first_pl, at("07:00"), seed.mastered_file(conn))
    seed.matched_play(conn, second_pl, at("10:00"), seed.library_file(conn))  # no master

    items = get_day(conn, st)
    rows = conn.execute(
        """SELECT position, play_event_id FROM station_day_plays
           WHERE station_id = %s AND play_date = %s""",
        (st, DAY),
    ).fetchall()
    by_position = {r["position"]: UUID(str(r["play_event_id"])) for r in rows}

    assert len(items) == len(by_position) == 5
    for index, item in enumerate(items):
        assert item.event_id == by_position[index]


@pytest.mark.parametrize("fault", ["negative-duration", "not-present"])
def test_bad_file_with_an_invalid_cue_row_logs_one_warning(conn: Conn, fault: str) -> None:
    """One warning per play: a bad final file's cues are not read, so no cue warning too."""
    st = seed.station(conn)
    audio = seed.audio_hash()
    bad = seed.mastered_file(conn, audio_hash=audio)
    seed.cue_row(conn, audio, fade_in_ms=-1)
    if fault == "negative-duration":
        conn.execute("UPDATE library_files SET duration_ms = -1 WHERE id = %s", (bad,))
    else:
        conn.execute("UPDATE library_files SET file_status = 'missing' WHERE id = %s", (bad,))
    event = seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), bad)
    expected = (
        "schedule_file_invalid" if fault == "negative-duration" else "schedule_file_unavailable"
    )

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(item.event_id, item.file) for item in items] == [(event, None)]
    assert [(e["event"], str(e["event_id"])) for e in logs if e["log_level"] == "warning"] == [
        (expected, str(event))
    ]
