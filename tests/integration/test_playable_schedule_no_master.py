"""Acceptance tests: the work always controls what plays (spec D22), at the reader.

A matched play reaches its work through its matched file (the file's own ``work_id``, else its
recording's work). The work's override for the station's format plays, else its song master.
The matched file never plays for being matched. No work, or a work with neither, leaves the
play unresolved: ``file=None`` and exactly one ``schedule_no_master`` warning carrying
``event_id``, ``matched_file_id`` and ``work_id`` (``None`` without a work). That check comes
first: such a play has no final file, so it is never judged on availability, length or cues.
An unmatched play has no file and logs nothing.

NEW for D22 (no locked counterpart); imports the D22 seed draft.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping, Sequence
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row
from structlog.testing import capture_logs

from backend.domain.streaming import ScheduleItem
from backend.services.repository_factory import RepositoryFactory
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import DAY, Conn, at


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def get_day(conn: Conn, station_id: UUID) -> list[ScheduleItem]:
    return RepositoryFactory(conn).streaming.schedule.get_day(station_id, DAY)


def all_warnings(logs: Sequence[MutableMapping[str, Any]]) -> list[MutableMapping[str, Any]]:
    return [e for e in logs if e["log_level"] == "warning"]


def file_on_work(conn: Conn, work_id: str | None, **fields: Any) -> UUID:
    """A library file whose own ``library_files.work_id`` is ``work_id``."""
    file_id = seed.library_file(conn, **fields)
    conn.execute("UPDATE library_files SET work_id = %s WHERE id = %s", (work_id, file_id))
    return file_id


def one_play(conn: Conn, matched: UUID, *, format_name: str | None = "AC") -> tuple[UUID, UUID]:
    """A station of ``format_name`` with one play whose one match is ``matched``."""
    st = seed.station(conn, format_name=format_name)
    return st, seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)


def assert_no_master(
    logs: Sequence[MutableMapping[str, Any]], event: UUID, matched: UUID, work: str | None
) -> None:
    """Exactly one warning in total: ``schedule_no_master`` for this play."""
    assert [
        (e["event"], str(e["event_id"]), str(e["matched_file_id"]), e["work_id"])
        for e in all_warnings(logs)
    ] == [("schedule_no_master", str(event), str(matched), work)]


# --- the work decides ------------------------------------------------------------------------


def test_master_plays_when_the_work_has_no_override(conn: Conn) -> None:
    """Requirement 1: the song master plays, not the matched file; nothing is logged."""
    work = seed.work(conn)
    matched, master = file_on_work(conn, work), file_on_work(conn, work)
    seed.song_master(conn, work, master)
    st, event = one_play(conn, matched)

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(i.event_id, i.file.file_id if i.file else None) for i in items] == [(event, master)]
    assert all_warnings(logs) == []


def test_override_plays_without_a_master(conn: Conn) -> None:
    """Requirement 2: the station format's override applies even when the work has no master."""
    work = seed.work(conn)
    matched, override = file_on_work(conn, work), file_on_work(conn, work)
    seed.format_override(conn, work, "AC", override)
    st, event = one_play(conn, matched)

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(i.event_id, i.file.file_id if i.file else None) for i in items] == [(event, override)]
    assert all_warnings(logs) == []


# --- no master: file=None and one warning ----------------------------------------------------


def test_work_with_neither_override_nor_master_gives_no_file_and_one_warning(conn: Conn) -> None:
    """Requirement 3: a present matched file on a masterless work does not play."""
    work = seed.work(conn)
    matched = file_on_work(conn, work)
    st, event = one_play(conn, matched)

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(i.event_id, i.file) for i in items] == [(event, None)]
    assert_no_master(logs, event, matched, work)


def test_override_for_another_format_only_is_no_master(conn: Conn) -> None:
    """An override for another format is not the station's: the work has nothing to play."""
    work = seed.work(conn)
    matched = file_on_work(conn, work)
    seed.format_override(conn, work, "CHR", file_on_work(conn, work))
    st, event = one_play(conn, matched, format_name="AC")

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(i.event_id, i.file) for i in items] == [(event, None)]
    assert_no_master(logs, event, matched, work)


@pytest.mark.parametrize("recording", [False, True], ids=["no-recording", "workless-recording"])
def test_matched_file_without_a_work_gives_no_master_with_no_work(
    conn: Conn, recording: bool
) -> None:
    """Requirement 4: neither the file nor its recording has a work: ``work_id`` is None."""
    recording_id = seed.recording(conn, None) if recording else None
    matched = file_on_work(conn, None, recording_id=recording_id)
    st, event = one_play(conn, matched)

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(i.event_id, i.file) for i in items] == [(event, None)]
    assert_no_master(logs, event, matched, None)


def test_no_master_through_the_recording_work_names_that_work(conn: Conn) -> None:
    """A workless file reaches its recording's work W (legacy guard); W has no master."""
    work = seed.work(conn)
    matched = file_on_work(conn, None, recording_id=seed.work_recording(conn, work))
    st, event = one_play(conn, matched)

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(i.event_id, i.file) for i in items] == [(event, None)]
    assert_no_master(logs, event, matched, work)


def test_no_per_level_fallback_warns_with_the_own_work(conn: Conn) -> None:
    """Test B at the reader: the recording's work has a master and an AC override, unused.

    The matched file's own work has neither, so the play has no master, named by that work.
    """
    own_work, recording_work = seed.work(conn), seed.work(conn)
    seed.song_master(conn, recording_work, seed.work_file(conn, recording_work))
    seed.format_override(conn, recording_work, "AC", seed.work_file(conn, recording_work))
    matched = file_on_work(conn, own_work, recording_id=seed.work_recording(conn, recording_work))
    st, event = one_play(conn, matched, format_name="AC")

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(i.event_id, i.file) for i in items] == [(event, None)]
    assert_no_master(logs, event, matched, own_work)


# --- one warning per play: no master comes first ---------------------------------------------


@pytest.mark.parametrize("fault", ["missing", "negative-duration", "invalid-cues", "all"])
def test_no_master_is_the_play_only_warning(conn: Conn, fault: str) -> None:
    """Requirement 6: the matched file's own faults are never judged: it is not the final file."""
    work = seed.work(conn)
    audio = seed.audio_hash()
    matched = file_on_work(
        conn,
        work,
        status="missing" if fault in ("missing", "all") else "present",
        duration_ms=-1 if fault in ("negative-duration", "all") else 200_000,
        audio_hash=audio,
    )
    seed.cue_row(conn, audio, fade_in_ms=-1 if fault in ("invalid-cues", "all") else 2_000)
    st, event = one_play(conn, matched)

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(i.event_id, i.file) for i in items] == [(event, None)]
    assert_no_master(logs, event, matched, work)


def test_no_master_play_keeps_its_place_in_the_day(conn: Conn) -> None:
    """Every logged play is returned: the no-master play stays between its neighbours."""
    st = seed.station(conn)
    pl = seed.playlist(conn, st)
    before, after = seed.mastered_file(conn), seed.mastered_file(conn)
    first = seed.matched_play(conn, pl, at("07:00"), before)
    masterless = seed.matched_play(conn, pl, at("08:00"), file_on_work(conn, seed.work(conn)))
    last = seed.matched_play(conn, pl, at("09:00"), after)

    items = get_day(conn, st)

    assert [(i.event_id, i.file.file_id if i.file else None) for i in items] == [
        (first, before),
        (masterless, None),
        (last, after),
    ]


# --- unmatched plays log nothing -------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "needs_review", "auto_rejected", "manual_rejected"])
def test_unmatched_identity_gives_no_file_and_no_warning(conn: Conn, status: str) -> None:
    """Requirement 7: a match row on an unmatched identity is not a route to any work."""
    st = seed.station(conn)
    ident = seed.identity(conn, status=status)
    seed.match(conn, ident, file_on_work(conn, seed.work(conn)))
    event = seed.play(conn, seed.playlist(conn, st), ident, at("08:00"))

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(i.event_id, i.file) for i in items] == [(event, None)]
    assert all_warnings(logs) == []


def test_only_fileless_matches_give_no_file_and_no_warning(conn: Conn) -> None:
    """A matched identity whose every match has no file has no matched file: not a no-master."""
    st = seed.station(conn)
    ident = seed.identity(conn)
    seed.match(conn, ident, None, confidence=0.9)
    seed.match(conn, ident, None, confidence=0.5)
    event = seed.play(conn, seed.playlist(conn, st), ident, at("08:00"))

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(i.event_id, i.file) for i in items] == [(event, None)]
    assert all_warnings(logs) == []


def test_matched_identity_without_a_match_row_gives_no_file_and_no_warning(conn: Conn) -> None:
    st = seed.station(conn)
    event = seed.play(conn, seed.playlist(conn, st), seed.identity(conn), at("08:00"))

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(i.event_id, i.file) for i in items] == [(event, None)]
    assert all_warnings(logs) == []
