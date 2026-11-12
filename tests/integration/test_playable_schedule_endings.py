"""The radio's two cue reads apply the ending rules over autocue (EOM ending rules, Task 5).

Precedence (spec "Non-goals", ARCHITECTURE.md "manual edits beat autocue"): a manual edit, then
the ending rule, then autocue's row. Both reads go through ``PgPlayableScheduleRepository``:
``get_day`` (the day read) and ``file_cues`` (the per-song re-read, D85). The day read also
carries the ending on ``PlayableFile.ending`` when the rule was applied, so the stream service
can shorten a long tail's fade before a liner (Task 7).

Each test builds its own station-day with plain INSERTs, as the edit-reader tests do.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.db.repositories.playable_schedule import PgPlayableScheduleRepository
from backend.db.repositories.stream_cue_edits import PgStreamCueEditRepository
from backend.db.repositories.stream_cues import PgStreamCueRepository
from backend.db.repositories.stream_endings import PgStreamEndingRepository
from backend.domain.endings import EndingType, Follows, SongEnding, ruled_cues
from backend.domain.library import AudioHash
from backend.domain.streaming import (
    CueAnalysis,
    CueMarkers,
    CuePoints,
    PlayableFile,
    edited_cues,
)

pytestmark = pytest.mark.integration

H = AudioHash.parse("flac-md5:" + "e" * 32)
DAY = date(2001, 1, 1)
T0 = datetime(2001, 1, 1, 12, 0, tzinfo=UTC)

STORED = CuePoints(
    cue_in_ms=0,
    cue_out_ms=200_000,
    fade_in_ms=0,
    fade_out_ms=4_000,
    start_next_ms=5_000,
    gain_db=-6.0,
)
LONG_TAIL = SongEnding(EndingType.LONG_TAIL, 190_500, 200_000)
COLD = SongEnding(EndingType.COLD, None, 200_000)
EDIT = CueMarkers(1_000, 194_000, 193_000, 199_000)


@pytest.fixture
def conn(migrated_db: str) -> Generator[psycopg.Connection[DictRow]]:
    """An autocommit dict-row connection with empty edits and endings (the shared truncation
    does not know these tables)."""
    with psycopg.connect(migrated_db, autocommit=True, row_factory=dict_row) as c:
        c.execute("DELETE FROM stream_cue_edits")
        c.execute("DELETE FROM stream_endings")
        yield c
        c.execute("DELETE FROM stream_cue_edits")
        c.execute("DELETE FROM stream_endings")


def _single_play(conn: psycopg.Connection[DictRow]) -> tuple[UUID, UUID]:
    """A station-day with one play of a song whose file carries ``H``: (station_id, file_id).

    The play resolves to its file through the identity's match, the file's work and that
    work's master.
    """
    station_id, playlist_id = uuid4(), uuid4()
    artist_id, identity_id, file_id = uuid4(), uuid4(), uuid4()
    mbid_artist, mbid_work = f"artist-{artist_id.hex}", f"work-{artist_id.hex}"
    conn.execute(
        "INSERT INTO stations (id, call_letters) VALUES (%s, %s)",
        (station_id, f"K{station_id.hex[:6]}"),
    )
    conn.execute(
        "INSERT INTO playlists (id, name, content_hash, station_id) VALUES (%s, %s, %s, %s)",
        (playlist_id, "show.csv", uuid4().hex, station_id),
    )
    conn.execute(
        """INSERT INTO broadcast_artists (id, original_name, normalized_name)
           VALUES (%s, 'Artist', %s)""",
        (artist_id, artist_id.hex),
    )
    conn.execute(
        """INSERT INTO track_identities
               (id, broadcast_artist_id, original_title, normalized_title,
                normalized_signature, match_status)
           VALUES (%s, %s, 'Song', 'song', %s, 'auto_matched')""",
        (identity_id, artist_id, identity_id.hex),
    )
    conn.execute(
        "INSERT INTO artists (id, name, sort_name) VALUES (%s, 'Artist', 'Artist')",
        (mbid_artist,),
    )
    conn.execute(
        "INSERT INTO works (id, title, artist_id) VALUES (%s, 'Song', %s)",
        (mbid_work, mbid_artist),
    )
    conn.execute(
        """INSERT INTO library_files (id, file_path, work_id, audio_hash, duration_ms,
                                      file_status)
           VALUES (%s, %s, %s, %s, 205000, 'present')""",
        (file_id, f"/music/{file_id}.flac", mbid_work, str(H)),
    )
    conn.execute(
        "INSERT INTO song_masters (work_id, preferred_file_id) VALUES (%s, %s)",
        (mbid_work, file_id),
    )
    conn.execute(
        "INSERT INTO matches (identity_id, library_file_id) VALUES (%s, %s)",
        (identity_id, file_id),
    )
    conn.execute(
        "INSERT INTO play_events (identity_id, playlist_id, played_at) VALUES (%s, %s, %s)",
        (identity_id, playlist_id, T0),
    )
    return station_id, file_id


def _autocue(conn: psycopg.Connection[DictRow], cues: CuePoints = STORED) -> None:
    PgStreamCueRepository(conn).upsert(
        CueAnalysis(
            audio_hash=H,
            cues=cues,
            loudness_lufs=-14.0,
            analysis_failed=False,
            analyser_version=1,
        )
    )


def _ending(conn: psycopg.Connection[DictRow], ending: SongEnding) -> None:
    PgStreamEndingRepository(conn).store(H, ending)


def _day_file(conn: psycopg.Connection[DictRow], station_id: UUID) -> PlayableFile:
    [item] = PgPlayableScheduleRepository(conn).get_day(station_id, DAY)
    assert item.file is not None
    return item.file


def test_day_read_applies_long_tail_rule(conn: psycopg.Connection[DictRow]) -> None:
    station_id, _ = _single_play(conn)
    _autocue(conn)
    _ending(conn, LONG_TAIL)
    file = _day_file(conn, station_id)
    assert file.cues == ruled_cues(STORED, LONG_TAIL, Follows.SONG)
    assert (file.cues.cue_out_ms, file.cues.fade_out_ms, file.cues.start_next_ms) == (
        197_000,  # autocue's EOM (195 s) and 2 s of fade (Lance, 2026-11-07)
        2_000,
        2_000,
    )
    assert file.ending == LONG_TAIL


def test_day_read_cold_drops_fade(conn: psycopg.Connection[DictRow]) -> None:
    station_id, _ = _single_play(conn)
    _autocue(conn)
    _ending(conn, COLD)
    file = _day_file(conn, station_id)
    assert file.cues is not None
    assert (file.cues.cue_out_ms, file.cues.fade_out_ms, file.cues.start_next_ms) == (
        200_000,
        0,
        5_000,
    )
    assert file.ending == COLD


def test_edit_beats_ending(conn: psycopg.Connection[DictRow]) -> None:
    station_id, file_id = _single_play(conn)
    _autocue(conn)
    _ending(conn, LONG_TAIL)
    PgStreamCueEditRepository(conn).save(H, EDIT)
    file = _day_file(conn, station_id)
    assert file.cues == edited_cues(EDIT, STORED)
    assert file.ending is None
    assert PgPlayableScheduleRepository(conn).file_cues(file_id) == edited_cues(EDIT, STORED)


def test_no_ending_row_is_autocue(conn: psycopg.Connection[DictRow]) -> None:
    station_id, file_id = _single_play(conn)
    _autocue(conn)
    file = _day_file(conn, station_id)
    assert file.cues == STORED
    assert file.ending is None
    assert PgPlayableScheduleRepository(conn).file_cues(file_id) == STORED


def test_stale_ending_is_autocue(conn: psycopg.Connection[DictRow]) -> None:
    station_id, file_id = _single_play(conn)
    _autocue(conn)
    _ending(conn, SongEnding(EndingType.LONG_TAIL, 190_500, 199_000))  # an older Stop
    file = _day_file(conn, station_id)
    assert file.cues == STORED
    assert file.ending is None  # not applied, so not carried
    assert PgPlayableScheduleRepository(conn).file_cues(file_id) == STORED


def test_file_cues_applies_rule(conn: psycopg.Connection[DictRow]) -> None:
    _, file_id = _single_play(conn)
    _autocue(conn)
    _ending(conn, LONG_TAIL)
    assert PgPlayableScheduleRepository(conn).file_cues(file_id) == ruled_cues(
        STORED, LONG_TAIL, Follows.SONG
    )
