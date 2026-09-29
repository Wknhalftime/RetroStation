"""Acceptance tests: one warning per play, and cues from the final file (D16, D18, D20-D22).

A play logs at most one warning, and which one follows a fixed order: no master first
(``schedule_no_master``, D22: the play has no final file), then availability
(``schedule_file_unavailable``: a file that is not present is never judged on its length or
its cues), then the file row itself (``schedule_file_invalid``), then its cues
(``schedule_cues_invalid``). Cues are those of the FINAL file's audio, never the matched
file's.

The reader is taken from ``RepositoryFactory``, as production wires it.

DRAFT for D22: replaces ``test_playable_schedule_warnings.py`` once the user approves.
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


def all_warnings(logs: Sequence[MutableMapping[str, Any]]) -> list[tuple[str, str, str]]:
    """Every warning logged, as (event, event_id, file_id)."""
    return [
        (e["event"], str(e.get("event_id")), str(e.get("file_id")))
        for e in logs
        if e["log_level"] == "warning"
    ]


def file_with_audio(
    conn: Conn, *, recording_id: str | None = None, **fields: Any
) -> tuple[UUID, str]:
    """A library file with a fresh ``audio_hash`` (and no cue row yet)."""
    audio = seed.audio_hash()
    return seed.library_file(conn, recording_id=recording_id, audio_hash=audio, **fields), audio


def test_invalid_cue_row_is_the_play_only_warning(conn: Conn) -> None:
    """A good, present file whose audio has an invalid cue row: one cue warning, nothing else."""
    st = seed.station(conn)
    audio = seed.audio_hash()
    file_id = seed.mastered_file(conn, audio_hash=audio)
    seed.cue_row(conn, audio, fade_in_ms=-1)
    event = seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), file_id)

    with capture_logs() as logs:
        [item] = get_day(conn, st)

    assert item.file is not None
    assert (item.file.file_id, item.file.cues) == (file_id, None)
    assert all_warnings(logs) == [("schedule_cues_invalid", str(event), str(file_id))]


@pytest.mark.parametrize("source", ["master", "override"])
def test_cues_are_those_of_the_resolved_file_audio(conn: Conn, source: str) -> None:
    """Every file in the chain has cues; only the final file's audio's cues are returned."""
    st = seed.station(conn, format_name="AC")
    work = seed.work(conn)
    recording = seed.work_recording(conn, work)
    matched, matched_audio = file_with_audio(conn, recording_id=recording)
    master, master_audio = file_with_audio(conn, recording_id=recording)
    seed.cue_row(conn, matched_audio, gain_db=-1.5)
    seed.cue_row(conn, master_audio, gain_db=-6.5)
    seed.song_master(conn, work, master)
    final, final_gain = master, -6.5
    if source == "override":
        override, override_audio = file_with_audio(conn, recording_id=recording)
        seed.cue_row(conn, override_audio, gain_db=-9.25)
        seed.format_override(conn, work, "AC", override)
        final, final_gain = override, -9.25
    seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)

    [item] = get_day(conn, st)

    assert item.file is not None
    assert item.file.file_id == final
    assert item.file.cues is not None
    assert item.file.cues.gain_db == final_gain


def test_unavailable_master_logs_one_warning_whatever_the_matched_cues(conn: Conn) -> None:
    """Neither invalid cue row is read: the play's one warning is the master's unavailability.

    The master's own row is invalid too, so reading the final file's cues before checking its
    status would add a ``schedule_cues_invalid`` warning.
    """
    st = seed.station(conn)
    work = seed.work(conn)
    recording = seed.work_recording(conn, work)
    matched, matched_audio = file_with_audio(conn, recording_id=recording)
    seed.cue_row(conn, matched_audio, fade_in_ms=-1)
    master, master_audio = file_with_audio(conn, recording_id=recording, status="missing")
    seed.cue_row(conn, master_audio, fade_in_ms=-1)
    seed.song_master(conn, work, master)
    event = seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(item.event_id, item.file) for item in items] == [(event, None)]
    assert all_warnings(logs) == [("schedule_file_unavailable", str(event), str(master))]
    [warning] = [e for e in logs if e["log_level"] == "warning"]
    assert (str(warning["source"]), str(warning["file_status"])) == ("master", "missing")


@pytest.mark.parametrize("source", ["matched-is-master", "other-master"])
def test_missing_file_with_negative_duration_logs_only_unavailable(conn: Conn, source: str) -> None:
    """User ruling: availability is checked first; a missing file is never judged on its length."""
    st = seed.station(conn)
    if source == "matched-is-master":
        matched = final = seed.mastered_file(conn, status="missing", duration_ms=-1)
    else:
        work = seed.work(conn)
        matched = seed.work_file(conn, work)
        final = seed.library_file(
            conn, status="missing", duration_ms=-1, recording_id=seed.work_recording(conn, work)
        )
        seed.song_master(conn, work, final)
    event = seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), matched)

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [(item.event_id, item.file) for item in items] == [(event, None)]
    assert all_warnings(logs) == [("schedule_file_unavailable", str(event), str(final))]
    [warning] = [e for e in logs if e["log_level"] == "warning"]
    assert (str(warning["source"]), str(warning["file_status"])) == ("master", "missing")
