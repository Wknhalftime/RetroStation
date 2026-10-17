"""Acceptance tests: D111 on PostgreSQL (S1, S3, S4, S4b, S7; proposal tests 6-8).

- stream_cues.audio_silent (BOOLEAN NOT NULL DEFAULT false) stores the marker, written by both
  write paths and replaced by a later analysis.
- Migration 0039 adds the column and deletes, once, the failed rows (S4) and the analysed rows
  already at the floor, gain_db >= +52.0 (S4b); its rollback in backend/db/ undoes it.
- The export's resolution read reports a silent final file (S7).
- At schedule resolution a silent row resolves the play to no file, with one
  schedule_file_silent warning naming it; the item stays in the day, and next_item /
  walk_forward land on the next playable song. Check order: no master, availability, file
  validity, silence, cues. A failed row that is not silent still plays (D52).
- D85 unchanged: the re-read of a silent row answers its fallback cues, with no warning.

Seeded through stream_seed's raw inserts; the reader is RepositoryFactory's, as production
wires it.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping, Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row
from structlog.testing import capture_logs

from backend.db.repositories.stream_cues import PgStreamCueRepository
from backend.domain.library import AudioHash
from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    CueAnalysis,
    CuePoints,
    ItemRef,
    Landing,
    ScheduleItem,
    StreamTiming,
)
from backend.domain.tune_in import next_item, tune_in, walk_forward
from backend.services.repository_factory import RepositoryFactory
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import DAY, Conn, at

_DB = Path(__file__).resolve().parents[2] / "backend" / "db"
_VERSION = "0039_stream_cues_audio_silent"
_MIGRATION = _DB / "migrations" / f"{_VERSION}.sql"
_ROLLBACK = _DB / f"rollback_{_VERSION}.sql"

FALLBACK = CuePoints(
    cue_in_ms=0,
    cue_out_ms=200_000,
    fade_in_ms=3_000,
    fade_out_ms=4_000,
    start_next_ms=0,
    gain_db=-8.0,
)
FALLBACK_ROW: dict[str, Any] = {
    "cue_in_ms": 0,
    "cue_out_ms": 200_000,
    "fade_in_ms": 3_000,
    "fade_out_ms": 4_000,
    "start_next_ms": 0,
    "gain_db": -8.0,
    "loudness_lufs": None,
    "analysis_failed": True,
}


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def get_day(conn: Conn, station_id: UUID) -> list[ScheduleItem]:
    return RepositoryFactory(conn).streaming.schedule.get_day(station_id, DAY)


def warnings(logs: Sequence[MutableMapping[str, Any]]) -> list[tuple[str, str, str]]:
    """Every warning logged, as (event, event_id, file_id)."""
    return [
        (e["event"], str(e.get("event_id")), str(e.get("file_id")))
        for e in logs
        if e["log_level"] == "warning"
    ]


def silent_master(conn: Conn, *, status: str = "present") -> UUID:
    """A file that is its work's master (D22), whose audio has a silent fallback row."""
    audio = seed.audio_hash()
    file_id = seed.mastered_file(conn, audio_hash=audio, status=status)
    seed.cue_row(conn, audio, **FALLBACK_ROW, audio_silent=True)
    return file_id


def marker(conn: Conn, audio: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT analysis_failed, audio_silent FROM stream_cues WHERE audio_hash = %s", (audio,)
    ).fetchone()


# --- The column and the migration (proposal test 8, S3, S4) -----------------------------------


def test_migration_0039_is_applied_and_adds_audio_silent(conn: Conn) -> None:
    applied = conn.execute(
        "SELECT 1 AS one FROM schema_migrations WHERE version = %s", (_VERSION,)
    ).fetchone()
    column = conn.execute(
        """SELECT data_type, is_nullable, column_default FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'stream_cues'
             AND column_name = 'audio_silent'"""
    ).fetchone()
    assert applied == {"one": 1}
    assert column == {"data_type": "boolean", "is_nullable": "NO", "column_default": "false"}


def test_audio_silent_defaults_to_false(conn: Conn) -> None:
    audio = seed.audio_hash()
    seed.cue_row(conn, audio)
    assert marker(conn, audio) == {"analysis_failed": False, "audio_silent": False}


def test_the_migration_deletes_the_failed_and_floor_rows_and_keeps_the_rest(
    migrated_db: str,
) -> None:
    """S4 and S4b: once, at deployment, every stored failed row goes, and so does every analysed
    row already at the floor (gain +52.0 dB, valid full-length cues), so each file is analysed
    again under D111. Every other row stays, the loudest real gain on dev (+46.022) included."""
    conn = psycopg.connect(migrated_db, row_factory=dict_row)
    try:
        conn.execute(_ROLLBACK.read_text(encoding="utf-8"))  # the schema before 0039
        analysed, failed, floor, loud = (seed.audio_hash() for _ in range(4))
        seed.cue_row(conn, analysed)
        seed.cue_row(conn, failed, **FALLBACK_ROW)
        seed.cue_row(
            conn,
            floor,
            cue_in_ms=0,
            cue_out_ms=120_000,
            fade_in_ms=0,
            fade_out_ms=400,
            start_next_ms=400,
            gain_db=52.0,
        )
        seed.cue_row(conn, loud, gain_db=46.022)
        conn.execute(_MIGRATION.read_text(encoding="utf-8"))
        rows = conn.execute("SELECT audio_hash, audio_silent FROM stream_cues").fetchall()
    finally:
        conn.rollback()  # DDL is transactional: the schema is left as migrated
        conn.close()
    assert sorted(rows, key=lambda r: r["audio_hash"]) == sorted(
        [
            {"audio_hash": analysed, "audio_silent": False},
            {"audio_hash": loud, "audio_silent": False},
        ],
        key=lambda r: r["audio_hash"],
    )


def test_the_rollback_script_undoes_the_migration(migrated_db: str) -> None:
    conn = psycopg.connect(migrated_db, row_factory=dict_row)
    try:
        conn.execute(_ROLLBACK.read_text(encoding="utf-8"))
        column = conn.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_name = 'stream_cues' AND column_name = 'audio_silent'"""
        ).fetchone()
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s", (_VERSION,)
        ).fetchone()
        conn.execute(_MIGRATION.read_text(encoding="utf-8"))  # it applies cleanly again
    finally:
        conn.rollback()
        conn.close()
    assert (column, applied) == (None, None)


# --- The store ---------------------------------------------------------------------------------


@pytest.mark.parametrize("write", ["store_if_current", "upsert"])
def test_a_silent_analysis_is_stored_with_its_marker(conn: Conn, write: str) -> None:
    audio = seed.audio_hash()
    file_id = seed.library_file(conn, audio_hash=audio)
    analysis = CueAnalysis(
        AudioHash.parse(audio), FALLBACK, None, True, CUE_ANALYSER_VERSION, audio_silent=True
    )
    store = PgStreamCueRepository(conn)
    if write == "store_if_current":
        assert store.store_if_current(analysis, file_id) is True
    else:
        store.upsert(analysis)
    assert marker(conn, audio) == {"analysis_failed": True, "audio_silent": True}


def test_a_new_analysis_replaces_the_silent_marker(conn: Conn) -> None:
    """One row per audio (Data): an analysis stored later (an analyser change) clears it."""
    audio = seed.audio_hash()
    file_id = seed.library_file(conn, audio_hash=audio)
    store = PgStreamCueRepository(conn)
    hashed = AudioHash.parse(audio)
    store.store_if_current(
        CueAnalysis(hashed, FALLBACK, None, True, CUE_ANALYSER_VERSION, audio_silent=True), file_id
    )
    store.store_if_current(
        CueAnalysis(hashed, seed.CUE_POINTS, None, False, CUE_ANALYSER_VERSION), file_id
    )
    assert marker(conn, audio) == {"analysis_failed": False, "audio_silent": False}


# --- The skip (proposal tests 6 and 7) ---------------------------------------------------------


def test_a_silent_song_resolves_to_no_file_and_the_walk_skips_it(conn: Conn) -> None:
    """Proposal test 6: no file, one schedule_file_silent warning naming it; the item keeps its
    place, and the walk and next-item land on the next playable song (D21's path)."""
    st = seed.station(conn)
    pl = seed.playlist(conn, st)
    before, _ = seed.cued_master(conn)
    silent = silent_master(conn)
    after, _ = seed.cued_master(conn)
    seed.matched_play(conn, pl, at("08:00"), before)
    event = seed.matched_play(conn, pl, at("08:05"), silent)
    seed.matched_play(conn, pl, at("08:10"), after)

    with capture_logs() as logs:
        items = get_day(conn, st)

    assert [None if i.file is None else i.file.file_id for i in items] == [before, None, after]
    assert warnings(logs) == [("schedule_file_silent", str(event), str(silent))]
    [warned] = [e for e in logs if e["event"] == "schedule_file_silent"]
    assert (warned["path"], warned["source"]) == (seed.file_path(conn, silent), "master")

    def load_day(day: date) -> list[ScheduleItem]:
        return items if day == DAY else []

    timing = StreamTiming()
    assert next_item(load_day, ItemRef(DAY, 0), timing) == ItemRef(DAY, 2)
    assert walk_forward(load_day, Landing(ItemRef(DAY, 1), 0), timing) == Landing(
        ItemRef(DAY, 2), 0
    )
    tuned = tune_in(load_day, DAY.year, datetime.combine(DAY, time(8, 6)), timing)
    assert tuned.landing == Landing(ItemRef(DAY, 2), 0)  # tune-in during it: the next song


def test_a_missing_file_is_unavailable_before_it_is_silent(conn: Conn) -> None:
    """One warning per play, in order: availability (D21) comes before silence."""
    st = seed.station(conn)
    missing = silent_master(conn, status="missing")
    event = seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), missing)
    with capture_logs() as logs:
        [item] = get_day(conn, st)
    assert item.file is None
    assert warnings(logs) == [("schedule_file_unavailable", str(event), str(missing))]


def test_an_invalid_file_is_invalid_before_it_is_silent(conn: Conn) -> None:
    """One warning per play, in order: file validity (D18) comes before silence."""
    st = seed.station(conn)
    bad = silent_master(conn)
    # The column has no CHECK, so a raw UPDATE stores the impossible value.
    conn.execute("UPDATE library_files SET duration_ms = -1 WHERE id = %s", (bad,))
    event = seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), bad)
    with capture_logs() as logs:
        [item] = get_day(conn, st)
    assert item.file is None
    assert warnings(logs) == [("schedule_file_invalid", str(event), str(bad))]


def test_a_failed_row_that_is_not_silent_still_plays(conn: Conn) -> None:
    """D52 unchanged: a fallback row for real audio plays whole, with no warning."""
    st = seed.station(conn)
    audio = seed.audio_hash()
    file_id = seed.mastered_file(conn, audio_hash=audio)
    seed.cue_row(conn, audio, **FALLBACK_ROW)
    seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), file_id)
    with capture_logs() as logs:
        [item] = get_day(conn, st)
    assert item.file is not None
    assert (item.file.file_id, item.file.cues) == (file_id, FALLBACK)
    assert warnings(logs) == []


def test_the_re_read_of_a_silent_row_gives_its_fallback_cues(conn: Conn) -> None:
    """Proposal test 7, D85 unchanged: the per-song re-read never fails or holds an item; a
    silent row answers its stored (fallback) cues, with no warning."""
    silent = silent_master(conn)
    with capture_logs() as logs:
        cues = RepositoryFactory(conn).streaming.schedule.file_cues(silent)
    assert cues == FALLBACK
    assert [e for e in logs if e["log_level"] == "warning"] == []


def test_a_silent_row_takes_its_audio_out_of_the_cue_work(conn: Conn) -> None:
    """Proposal test 4 on PostgreSQL (D61): the stored silent row settles its audio, so the
    library backlog, the priority set and a report (D79) never offer the file again."""
    st = seed.station(conn)
    silent = silent_master(conn)
    seed.matched_play(conn, seed.playlist(conn, st), at("08:00"), silent)
    work = RepositoryFactory(conn).streaming.cue_work
    assert (work.library(None, 10), work.scheduled([DAY]), work.reported(silent)) == ([], [], None)


# --- The export's resolution read (S7) ---------------------------------------------------------


def test_the_resolution_read_reports_a_silent_final_file(conn: Conn) -> None:
    """S7: the M3U export's read joins the final file's audio's row, as the schedule reader
    does; audio with no row, or a row that is not silent, reads not silent."""
    st = seed.station(conn)
    pl = seed.playlist(conn, st)
    silent = silent_master(conn)
    cued, _ = seed.cued_master(conn)
    uncued = seed.mastered_file(conn, audio_hash=seed.audio_hash())
    events = {
        seed.matched_play(conn, pl, at("08:00"), silent): True,
        seed.matched_play(conn, pl, at("08:05"), cued): False,
        seed.matched_play(conn, pl, at("08:10"), uncued): False,
    }
    got = RepositoryFactory(conn).play_file_resolution.get_for_plays(list(events))
    assert {event: r.audio_silent for event, r in got.items()} == events
    assert got[next(iter(events))].file_id == silent
