"""The "needs analysis" reads against PostgreSQL.

Spec: D20 ("needs analysis" is "has an audio_hash and no stream_cues row"); D21 (playable is
file_status = 'present'); D17/D22 (the final file is read from play_file_resolution, per play,
through the fenced lateral); D19/D3 (station days from station_day_plays, on the UTC-label
date); D62 (every station, every year from the first to the last logged play); review I4 (the
priority set is read once per run).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import cast
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.stream_cue_work import PgCueWorkRepository
from backend.domain.library import AudioHash
from backend.domain.streaming import CueCandidate
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import DAY, Conn
from tests.integration.test_playable_schedule_fence import (
    PLANNER_SETTINGS,
    VIEW_TABLES,
    StatementRecorder,
    looks_up_each_play,
    outline,
    plan_of,
    tables,
    view_inside_loops,
)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def hashed_file(
    conn: Conn, *, status: str = "present", duration_ms: int | None = 200_000
) -> tuple[UUID, str]:
    audio = seed.audio_hash()
    file_id = seed.library_file(conn, audio_hash=audio, status=status, duration_ms=duration_ms)
    return file_id, audio


def hashed_master(conn: Conn, *, status: str = "present") -> UUID:
    return seed.mastered_file(conn, audio_hash=seed.audio_hash(), status=status)


def by_path(conn: Conn, file_ids: list[UUID]) -> list[UUID]:
    return sorted(file_ids, key=lambda f: seed.file_path(conn, f))


def test_library_lists_present_hashed_audio_with_no_row_in_path_order(conn: Conn) -> None:
    """D20 needs analysis; D21 present only."""
    wanted = [hashed_file(conn)[0] for _ in range(3)]
    seed.cued_file(conn)
    seed.library_file(conn)  # no audio_hash: no cues, ever (D20)
    hashed_file(conn, status="missing")
    found = PgCueWorkRepository(conn).library(None, 10)
    assert [c.file_id for c in found] == by_path(conn, wanted)


def test_library_continues_after_the_cursor_and_stops_at_the_limit(conn: Conn) -> None:
    """D63: bounded keyset batches, as in the hash backfill."""
    files = by_path(conn, [hashed_file(conn)[0] for _ in range(4)])
    work = PgCueWorkRepository(conn)
    first = work.library(None, 2)
    rest = work.library(first[-1].path, 10)
    assert [c.file_id for c in first] == files[:2]
    assert [c.file_id for c in rest] == files[2:]


def test_a_candidate_carries_what_the_analysis_needs(conn: Conn) -> None:
    """The hash read before analysing, the duration for a fallback row (D52), and the stat
    the library trusts the hash by (D57)."""
    file_id, audio = hashed_file(conn, duration_ms=None)
    [found] = PgCueWorkRepository(conn).library(None, 10)
    assert found == CueCandidate(
        file_id=file_id,
        path=seed.file_path(conn, file_id),
        audio_hash=AudioHash.parse(audio),
        duration_ms=None,
        file_size=seed.FILE_SIZE,
        file_mtime_ns=seed.FILE_MTIME_NS,
    )


def test_scheduled_lists_the_final_files_of_plays_on_the_days(conn: Conn) -> None:
    """D62: the files that play on the given days, resolved by play_file_resolution (D22: the
    work's master plays, never the matched file), present (D21), needing analysis (D20),
    each once, at every station."""
    playlist = seed.playlist(conn, seed.station(conn))
    elsewhere = seed.playlist(conn, seed.station(conn, format_name="CHR"))
    other_station = hashed_master(conn)
    seed.matched_play(conn, elsewhere, seed.at("12:00:00"), other_station)
    plays_today = hashed_master(conn)
    seed.matched_play(conn, playlist, seed.at("06:00:00"), plays_today)
    seed.matched_play(conn, playlist, seed.at("09:00:00"), plays_today)
    work_id = seed.work(conn)
    matched, _ = hashed_file(conn)
    master, _ = hashed_file(conn)
    conn.execute(
        "UPDATE library_files SET work_id = %s WHERE id = ANY(%s)", (work_id, [matched, master])
    )
    seed.song_master(conn, work_id, master)
    seed.matched_play(conn, playlist, seed.at("06:04:00"), matched)
    tomorrow = hashed_master(conn)
    seed.matched_play(conn, playlist, seed.at("00:10:00", DAY + timedelta(days=1)), tomorrow)
    later = hashed_master(conn)
    seed.matched_play(conn, playlist, seed.at("06:00:00", DAY + timedelta(days=2)), later)
    cued, _ = seed.cued_master(conn)
    seed.matched_play(conn, playlist, seed.at("07:00:00"), cued)
    gone = hashed_master(conn, status="missing")
    seed.matched_play(conn, playlist, seed.at("08:00:00"), gone)
    hashed_file(conn)  # in the library, never scheduled
    found = PgCueWorkRepository(conn).scheduled([DAY, DAY + timedelta(days=1)])
    wanted = [plays_today, master, tomorrow, other_station]
    assert [c.file_id for c in found] == by_path(conn, wanted)


def test_scheduled_days_are_utc_label_dates_whatever_the_session_time_zone(conn: Conn) -> None:
    """D3/D19: the station day is (played_at AT TIME ZONE 'UTC')::date."""
    conn.execute("SET TIME ZONE 'America/Chicago'")
    playlist = seed.playlist(conn, seed.station(conn))
    late = hashed_master(conn)
    seed.matched_play(conn, playlist, seed.at("23:30:00"), late)
    early_next = hashed_master(conn)
    seed.matched_play(conn, playlist, seed.at("00:30:00", DAY + timedelta(days=1)), early_next)
    found = PgCueWorkRepository(conn).scheduled([DAY])
    assert [c.file_id for c in found] == [late]


def test_logged_years_span_the_first_and_last_logged_play(conn: Conn) -> None:
    """D62: every year from the first to the last logged play, on the UTC-label date (D3),
    whatever the session time zone."""
    conn.execute("SET TIME ZONE 'America/Chicago'")
    playlist = seed.playlist(conn, seed.station(conn))
    song = hashed_master(conn)
    seed.matched_play(conn, playlist, seed.at("06:00:00", date(1995, 3, 14)), song)
    seed.matched_play(conn, playlist, seed.at("00:30:00", date(1998, 1, 1)), song)
    assert PgCueWorkRepository(conn).logged_years() == range(1995, 1999)


def test_logged_years_is_empty_with_no_plays(conn: Conn) -> None:
    """No log, no station-years."""
    assert PgCueWorkRepository(conn).logged_years() == range(0)


@pytest.mark.parametrize("settings", PLANNER_SETTINGS.values(), ids=PLANNER_SETTINGS.keys())
def test_scheduled_resolves_each_play_through_the_fenced_lateral(
    conn: Conn, settings: dict[str, str]
) -> None:
    """D17/D22 consumption contract: play_file_resolution is evaluated per play (about 8 ms a
    day on dev), never for every play (about 10 s)."""
    recorder = StatementRecorder(conn)
    PgCueWorkRepository(cast(Conn, recorder)).scheduled([DAY])
    for name, value in settings.items():
        conn.execute("SELECT set_config(%s, %s, true)", (name, value))
    plans = [plan_of(conn, query, params) for query, params in recorder.statements]
    resolving = [plan for plan in plans if VIEW_TABLES.issubset(tables(plan))]
    assert resolving, "scheduled no longer reads play_file_resolution"
    for plan in resolving:
        assert any(looks_up_each_play(inner) for inner in view_inside_loops(plan)), (
            "play_file_resolution is not evaluated per play:\n" + outline(plan)
        )
