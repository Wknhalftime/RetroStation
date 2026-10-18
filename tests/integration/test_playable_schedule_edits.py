"""Cue editor PR 1, Task 3: the radio's two cue reads prefer a manual edit over autocue.

Both paths are tested through ``PgPlayableScheduleRepository``'s public methods:
``get_day`` (the day reader) and ``file_cues`` (the per-song re-read, D85).

Requirement R5 (the radio plays the edit on both reads, with the gain rule of plan note 6),
R4 (edits are keyed by audio_hash), anti-goal 2 (radio = preview), Review Focus 1 (twin
files share an edit) and Review Focus 2 (an edit still plays when the autocue row is gone).

Each test builds its own station-day with plain INSERTs: a play resolves to its file through
the identity's match, the file's work and that work's master (``song_masters``).
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.db.repositories.playable_schedule import PgPlayableScheduleRepository
from backend.db.repositories.stream_cue_edits import PgStreamCueEditRepository
from backend.db.repositories.stream_cues import PgStreamCueRepository
from backend.domain.enums import FileStatus
from backend.domain.library import AudioHash
from backend.domain.streaming import CueAnalysis, CueMarkers, CuePoints, ScheduleItem

pytestmark = pytest.mark.integration

H = AudioHash.parse("flac-md5:" + "a" * 32)
H2 = AudioHash.parse("flac-md5:" + "b" * 32)
H3 = AudioHash.parse("flac-md5:" + "c" * 32)

# Heretic Anthem's autocue row on dev, 2026-10-17.
HERETIC = CuePoints(
    cue_in_ms=0,
    cue_out_ms=247_100,
    fade_in_ms=0,
    fade_out_ms=6_000,
    start_next_ms=6_000,
    gain_db=-11.63,
)
# Another song's autocue row, to tell one audio's cues from another's.
OTHER = CuePoints(
    cue_in_ms=1_000,
    cue_out_ms=200_000,
    fade_in_ms=0,
    fade_out_ms=5_000,
    start_next_ms=3_000,
    gain_db=-6.5,
)
# The edit used by most tests: Start 9 s, EOM and Stop as autocue, Fade-out earlier.
EDIT = CueMarkers(9_000, 241_100, 240_000, 247_100)
EDITED = CuePoints(
    cue_in_ms=9_000,
    cue_out_ms=247_100,
    fade_in_ms=0,
    fade_out_ms=7_100,
    start_next_ms=6_000,
    gain_db=-11.63,
)

DAY = date(2001, 1, 1)
T0 = datetime(2001, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(migrated_db: str) -> Generator[psycopg.Connection[DictRow]]:
    """An autocommit dict-row connection with an empty ``stream_cue_edits``.

    The shared ``migrated_db`` truncation does not know this table yet, so the fixture
    clears it before and after each test.
    """
    with psycopg.connect(migrated_db, autocommit=True, row_factory=dict_row) as c:
        c.execute("DELETE FROM stream_cue_edits")
        yield c
        c.execute("DELETE FROM stream_cue_edits")


def _station(conn: psycopg.Connection[DictRow]) -> tuple[UUID, UUID]:
    """A station and its playlist; returns (station_id, playlist_id)."""
    station_id, playlist_id = uuid4(), uuid4()
    conn.execute(
        "INSERT INTO stations (id, call_letters) VALUES (%s, %s)",
        (station_id, f"K{station_id.hex[:6]}"),
    )
    conn.execute(
        "INSERT INTO playlists (id, name, content_hash, station_id) VALUES (%s, %s, %s, %s)",
        (playlist_id, "show.csv", uuid4().hex, station_id),
    )
    return station_id, playlist_id


def _song(
    conn: psycopg.Connection[DictRow],
    audio_hash: AudioHash | None,
    *,
    status: FileStatus = FileStatus.PRESENT,
) -> tuple[UUID, UUID]:
    """A matched identity whose work's master is a new library file carrying ``audio_hash``.

    Returns (identity_id, file_id). Two calls with the same hash make two files that share
    one audio (twins), each the master of its own work.
    """
    artist_id, identity_id, file_id = uuid4(), uuid4(), uuid4()
    mbid_artist, mbid_work = f"artist-{artist_id.hex}", f"work-{artist_id.hex}"
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
           VALUES (%s, %s, %s, %s, 250000, %s)""",
        (
            file_id,
            f"/music/{file_id}.flac",
            mbid_work,
            None if audio_hash is None else str(audio_hash),
            str(status),
        ),
    )
    conn.execute(
        "INSERT INTO song_masters (work_id, preferred_file_id) VALUES (%s, %s)",
        (mbid_work, file_id),
    )
    conn.execute(
        "INSERT INTO matches (identity_id, library_file_id) VALUES (%s, %s)",
        (identity_id, file_id),
    )
    return identity_id, file_id


def _play(
    conn: psycopg.Connection[DictRow], identity_id: UUID, playlist_id: UUID, minute: int
) -> None:
    conn.execute(
        "INSERT INTO play_events (identity_id, playlist_id, played_at) VALUES (%s, %s, %s)",
        (identity_id, playlist_id, T0 + timedelta(minutes=minute)),
    )


def _autocue(conn: psycopg.Connection[DictRow], audio_hash: AudioHash, cues: CuePoints) -> None:
    PgStreamCueRepository(conn).upsert(
        CueAnalysis(
            audio_hash=audio_hash,
            cues=cues,
            loudness_lufs=-14.5,
            analysis_failed=False,
            analyser_version=1,
        )
    )


def _edit(conn: psycopg.Connection[DictRow], audio_hash: AudioHash, markers: CueMarkers) -> None:
    PgStreamCueEditRepository(conn).save(audio_hash, markers)


def _day(conn: psycopg.Connection[DictRow], station_id: UUID) -> list[ScheduleItem]:
    return PgPlayableScheduleRepository(conn).get_day(station_id, DAY)


def _single_play(
    conn: psycopg.Connection[DictRow], audio_hash: AudioHash | None
) -> tuple[UUID, UUID]:
    """A station-day with one play of a song whose file carries ``audio_hash``.

    Returns (station_id, file_id).
    """
    station_id, playlist_id = _station(conn)
    identity_id, file_id = _song(conn, audio_hash)
    _play(conn, identity_id, playlist_id, 0)
    return station_id, file_id


def test_day_read_uses_the_edit_over_autocue(conn: psycopg.Connection[DictRow]):
    """R5, anti-goal 2: with an edit and an autocue row, the day read plays the edit.

    Start, Fade-out, Stop and EOM come from the edit; gain stays the autocue row's.
    """
    station_id, file_id = _single_play(conn, H)
    _autocue(conn, H, HERETIC)
    _edit(conn, H, EDIT)

    [item] = _day(conn, station_id)

    assert item.file is not None
    assert item.file.file_id == file_id
    assert item.file.cues == EDITED


def test_edit_without_autocue_row_plays_at_no_cues_gain(conn: psycopg.Connection[DictRow]):
    """R5, R4, Review Focus 2: with the autocue row gone, the edit plays at 0 dB."""
    station_id, _ = _single_play(conn, H)
    _edit(conn, H, EDIT)

    [item] = _day(conn, station_id)

    assert item.file is not None
    assert item.file.cues == EDIT.to_points(0.0)


def test_edit_takes_the_gain_of_a_failed_analysis_row(conn: psycopg.Connection[DictRow]):
    """R5, plan note 6: an edit never changes loudness, including a D52 fallback row's -8 dB."""
    station_id, _ = _single_play(conn, H)
    fallback = CuePoints(
        cue_in_ms=0,
        cue_out_ms=250_000,
        fade_in_ms=0,
        fade_out_ms=4_000,
        start_next_ms=0,
        gain_db=-8.0,
    )
    PgStreamCueRepository(conn).upsert(
        CueAnalysis(
            audio_hash=H,
            cues=fallback,
            loudness_lufs=None,
            analysis_failed=True,
            analyser_version=1,
        )
    )
    _edit(conn, H, EDIT)

    [item] = _day(conn, station_id)

    assert item.file is not None
    assert item.file.cues == EDIT.to_points(-8.0)


def test_no_edit_reads_autocue_unchanged(conn: psycopg.Connection[DictRow]):
    """Regression (R5): a song without an edit still reads its autocue row as before."""
    station_id, _ = _single_play(conn, H)
    _autocue(conn, H, HERETIC)

    [item] = _day(conn, station_id)

    assert item.file is not None
    assert item.file.cues == HERETIC


def test_no_edit_and_no_autocue_row_reads_no_cues(conn: psycopg.Connection[DictRow]):
    """Regression (R5): neither edit nor row leaves the file uncued (D23 plays instead)."""
    station_id, _ = _single_play(conn, H)

    [item] = _day(conn, station_id)

    assert item.file is not None
    assert item.file.cues is None


def test_an_edit_leaves_other_songs_on_autocue(conn: psycopg.Connection[DictRow]):
    """R4: an edit applies to its own audio only, in the same day as songs without one."""
    station_id, playlist_id = _station(conn)
    edited_identity, _ = _song(conn, H)
    plain_identity, _ = _song(conn, H2)
    nothing_identity, _ = _song(conn, H3)
    _play(conn, edited_identity, playlist_id, 0)
    _play(conn, plain_identity, playlist_id, 1)
    _play(conn, nothing_identity, playlist_id, 2)
    _autocue(conn, H, HERETIC)
    _autocue(conn, H2, OTHER)
    _edit(conn, H, EDIT)

    items = _day(conn, station_id)

    assert [i.file.cues if i.file else "no file" for i in items] == [EDITED, OTHER, None]


def test_reread_uses_the_edit(conn: psycopg.Connection[DictRow]):
    """R5: the D85 re-read of a file's cues returns the edit, not autocue's row."""
    _, file_id = _single_play(conn, H)
    _autocue(conn, H, HERETIC)
    _edit(conn, H, EDIT)

    assert PgPlayableScheduleRepository(conn).file_cues(file_id) == EDITED


def test_reread_with_edit_and_no_autocue_row(conn: psycopg.Connection[DictRow]):
    """R5, Review Focus 2: with only an edit the re-read gives the edit at 0 dB, not None."""
    _, file_id = _single_play(conn, H)
    _edit(conn, H, EDIT)

    assert PgPlayableScheduleRepository(conn).file_cues(file_id) == EDIT.to_points(0.0)


def test_reread_without_edit_reads_autocue_unchanged(conn: psycopg.Connection[DictRow]):
    """Regression (R5): the re-read of a song without an edit is autocue's row."""
    _, file_id = _single_play(conn, H)
    _autocue(conn, H, HERETIC)

    assert PgPlayableScheduleRepository(conn).file_cues(file_id) == HERETIC


def test_reread_with_neither_edit_nor_row_is_none(conn: psycopg.Connection[DictRow]):
    """Regression (R5): no edit and no row still answers no cues (D85 makes the file uncued)."""
    _, file_id = _single_play(conn, H)

    assert PgPlayableScheduleRepository(conn).file_cues(file_id) is None


def test_reread_of_an_unknown_file_is_none(conn: psycopg.Connection[DictRow]):
    """Regression (R5): an edit for some audio does not give an unknown file id any cues."""
    _single_play(conn, H)
    _edit(conn, H, EDIT)

    assert PgPlayableScheduleRepository(conn).file_cues(uuid4()) is None


def test_twin_files_share_one_edit(conn: psycopg.Connection[DictRow]):
    """R4, Review Focus 1: two present files with the same audio hash both play the edit."""
    station_id, playlist_id = _station(conn)
    first_identity, first_file = _song(conn, H)
    second_identity, second_file = _song(conn, H)
    _play(conn, first_identity, playlist_id, 0)
    _play(conn, second_identity, playlist_id, 1)
    _autocue(conn, H, HERETIC)
    _edit(conn, H, EDIT)

    items = _day(conn, station_id)

    assert [i.file.file_id if i.file else None for i in items] == [first_file, second_file]
    assert [i.file.cues if i.file else None for i in items] == [EDITED, EDITED]
    repo = PgPlayableScheduleRepository(conn)
    assert repo.file_cues(first_file) == EDITED
    assert repo.file_cues(second_file) == EDITED


def test_edit_on_old_hash_is_ignored_after_audio_changes(conn: psycopg.Connection[DictRow]):
    """R4: a file re-hashed to different audio plays that audio's autocue, not the old edit."""
    station_id, file_id = _single_play(conn, H)
    _autocue(conn, H, HERETIC)
    _edit(conn, H, EDIT)
    _autocue(conn, H2, OTHER)
    conn.execute("UPDATE library_files SET audio_hash = %s WHERE id = %s", (str(H2), file_id))

    [item] = _day(conn, station_id)

    assert item.file is not None
    assert item.file.cues == OTHER
    assert PgPlayableScheduleRepository(conn).file_cues(file_id) == OTHER


def test_edit_on_old_hash_gives_no_cues_when_the_new_audio_has_none(
    conn: psycopg.Connection[DictRow],
):
    """R4: re-hashed to audio with no row and no edit, the old edit does not follow the file."""
    station_id, file_id = _single_play(conn, H)
    _edit(conn, H, EDIT)
    conn.execute("UPDATE library_files SET audio_hash = %s WHERE id = %s", (str(H3), file_id))

    [item] = _day(conn, station_id)

    assert item.file is not None
    assert item.file.cues is None
    assert PgPlayableScheduleRepository(conn).file_cues(file_id) is None


def test_file_without_audio_hash_reads_no_cues(conn: psycopg.Connection[DictRow]):
    """R4: a file with no fingerprint holds no edit, whatever edits exist for other audio."""
    station_id, file_id = _single_play(conn, None)
    _edit(conn, H, EDIT)

    [item] = _day(conn, station_id)

    assert item.file is not None
    assert item.file.cues is None
    assert PgPlayableScheduleRepository(conn).file_cues(file_id) is None


def test_edit_does_not_make_a_missing_file_playable(conn: psycopg.Connection[DictRow]):
    """R5 (D21): an edit changes cues only; a file that is not present still resolves to none."""
    station_id, playlist_id = _station(conn)
    identity_id, _ = _song(conn, H, status=FileStatus.MISSING)
    _play(conn, identity_id, playlist_id, 0)
    _edit(conn, H, EDIT)

    [item] = _day(conn, station_id)

    assert item.file is None
