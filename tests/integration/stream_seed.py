"""Seed helpers for the playable-schedule and stream-cue tests (spec: Data, D3, D16, D20, D22).

One row per call with explicit values, so each test states the chain it needs:
station → playlist → play → identity → match → file → recording → work → master/override,
plus raw ``stream_cues`` rows keyed by audio hash (raw so that a test can store a row the
write model would reject).

Under D22 a matched file never plays because it was matched: its work decides. A test that
wants "this file plays" seeds it with ``mastered_file``, a file that is its own work's master.

DRAFT for D22: replaces ``stream_seed.py`` once the user approves; then renamed back.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql as pg_sql
from psycopg.rows import DictRow

from backend.domain.streaming import CuePoints

type Conn = psycopg.Connection[DictRow]

DAY = date(1995, 3, 14)
VERSION = 3
"""The analyser version seeded cue rows record. The reader never checks it (D20)."""

FILE_SIZE = 4_000_000
FILE_MTIME_NS = 800_000_000_000_000_000


def at(hms: str, on: date = DAY) -> datetime:
    """A ``played_at``: station wall-clock ``hms`` on ``on``, stored under the UTC label (D3)."""
    return datetime.combine(on, time.fromisoformat(hms), tzinfo=UTC)


def wall(hms: str, on: date = DAY) -> datetime:
    """The naive ``logged_at`` the reader must return for ``at(hms, on)``."""
    return datetime.combine(on, time.fromisoformat(hms))


def audio_hash() -> str:
    """A fresh, well-formed ``library_files.audio_hash`` value (``flac-md5:<32 hex>``)."""
    return f"flac-md5:{uuid4().hex}"


def station(conn: Conn, format_name: str | None = "AC") -> UUID:
    row = conn.execute(
        "INSERT INTO stations (call_letters, format_name) VALUES (%s, %s) RETURNING id",
        (f"K{uuid4().hex[:8].upper()}", format_name),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def playlist(conn: Conn, station_id: UUID) -> UUID:
    row = conn.execute(
        "INSERT INTO playlists (name, content_hash, station_id) VALUES (%s, %s, %s) RETURNING id",
        (f"log-{uuid4()}.csv", uuid4().hex, station_id),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def identity(
    conn: Conn,
    *,
    title: str = "Title",
    artist: str = "Artist",
    status: str = "auto_matched",
    identity_id: UUID | None = None,
) -> UUID:
    artist_row = conn.execute(
        """INSERT INTO broadcast_artists (original_name, normalized_name)
           VALUES (%s, %s) RETURNING id""",
        (artist, f"{artist.lower()} {uuid4().hex}"),  # normalized_name is unique
    ).fetchone()
    assert artist_row is not None
    row = conn.execute(
        """INSERT INTO track_identities
               (id, broadcast_artist_id, original_title, normalized_title,
                normalized_signature, match_status)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (
            identity_id or uuid4(),
            artist_row["id"],
            title,
            title.lower(),
            uuid4().hex,
            status,
        ),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def work(conn: Conn) -> str:
    artist_id = f"artist-{uuid4()}"
    conn.execute(
        "INSERT INTO artists (id, name, sort_name) VALUES (%s, %s, %s)",
        (artist_id, "Artist", "Artist"),
    )
    work_id = f"work-{uuid4()}"
    conn.execute(
        "INSERT INTO works (id, title, artist_id) VALUES (%s, %s, %s)",
        (work_id, "Work", artist_id),
    )
    return work_id


def recording(conn: Conn, work_id: str | None) -> str:
    recording_id = f"rec-{uuid4()}"
    conn.execute(
        "INSERT INTO recordings (id, title, work_id) VALUES (%s, %s, %s)",
        (recording_id, "Recording", work_id),
    )
    return recording_id


def library_file(
    conn: Conn,
    *,
    status: str = "present",
    duration_ms: int | None = 200_000,
    recording_id: str | None = None,
    file_size: int | None = FILE_SIZE,
    file_mtime_ns: int | None = FILE_MTIME_NS,
    audio_hash: str | None = None,
) -> UUID:
    row = conn.execute(
        """INSERT INTO library_files
               (file_path, file_status, duration_ms, recording_id, file_size, file_mtime_ns,
                audio_hash)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (
            f"D:/music/{uuid4()}.flac",
            status,
            duration_ms,
            recording_id,
            file_size,
            file_mtime_ns,
            audio_hash,
        ),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def file_path(conn: Conn, file_id: UUID) -> str:
    row = conn.execute("SELECT file_path FROM library_files WHERE id = %s", (file_id,)).fetchone()
    assert row is not None
    return str(row["file_path"])


def work_recording(conn: Conn, work_id: str) -> str:
    """The recording of ``work_id``: one per work (recordings are unique per work and
    version type), created on first use."""
    row = conn.execute("SELECT id FROM recordings WHERE work_id = %s", (work_id,)).fetchone()
    return str(row["id"]) if row is not None else recording(conn, work_id)


def work_file(conn: Conn, work_id: str, *, status: str = "present") -> UUID:
    """A file of the recording of ``work_id``."""
    return library_file(conn, status=status, recording_id=work_recording(conn, work_id))


def song_master(conn: Conn, work_id: str, file_id: UUID) -> None:
    conn.execute(
        "INSERT INTO song_masters (work_id, preferred_file_id) VALUES (%s, %s)",
        (work_id, file_id),
    )


def format_override(conn: Conn, work_id: str, format_name: str, file_id: UUID) -> None:
    conn.execute(
        """INSERT INTO format_overrides (work_id, format_name, preferred_file_id)
           VALUES (%s, %s, %s)""",
        (work_id, format_name, file_id),
    )


def mastered_file(conn: Conn, **file_fields: Any) -> UUID:
    """A library file on a work of its own whose song master is the file itself (D22).

    Matched, it plays as its work's master. ``file_fields`` go to ``library_file``.
    """
    work_id = work(conn)
    file_id = library_file(conn, **file_fields)
    conn.execute("UPDATE library_files SET work_id = %s WHERE id = %s", (work_id, file_id))
    song_master(conn, work_id, file_id)
    return file_id


def match(
    conn: Conn,
    identity_id: UUID,
    file_id: UUID | None,
    *,
    confidence: float = 0.9,
    created_at: datetime | None = None,
    match_id: UUID | None = None,
) -> UUID:
    row = conn.execute(
        """INSERT INTO matches (id, identity_id, library_file_id, confidence_score, created_at)
           VALUES (%s, %s, %s, %s, COALESCE(%s, now())) RETURNING id""",
        (match_id or uuid4(), identity_id, file_id, confidence, created_at),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def play(
    conn: Conn,
    playlist_id: UUID,
    identity_id: UUID,
    played_at: datetime,
    *,
    event_id: UUID | None = None,
) -> UUID:
    row = conn.execute(
        """INSERT INTO play_events (id, identity_id, playlist_id, played_at)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        (event_id or uuid4(), identity_id, playlist_id, played_at),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def matched_play(
    conn: Conn,
    playlist_id: UUID,
    played_at: datetime,
    file_id: UUID,
    *,
    title: str = "Title",
    artist: str = "Artist",
) -> UUID:
    """A play of a fresh ``auto_matched`` identity whose one match is ``file_id``."""
    identity_id = identity(conn, title=title, artist=artist)
    match(conn, identity_id, file_id)
    return play(conn, playlist_id, identity_id, played_at)


CUE_ROW: dict[str, Any] = {
    "cue_in_ms": 1_000,
    "cue_out_ms": 181_000,
    "fade_in_ms": 2_000,
    "fade_out_ms": 5_000,
    "start_next_ms": 4_000,
    "loudness_lufs": -14.25,
    "gain_db": -3.75,
    "analyser_version": VERSION,
    "analysis_failed": False,
}
"""A valid cue row, all but its ``audio_hash``."""

CUE_POINTS = CuePoints(
    cue_in_ms=1_000,
    cue_out_ms=181_000,
    fade_in_ms=2_000,
    fade_out_ms=5_000,
    start_next_ms=4_000,
    gain_db=-3.75,
)
"""The ``CuePoints`` that ``CUE_ROW`` describes."""


def cue_row(conn: Conn, audio_hash: str, **overrides: object) -> None:
    """Insert a raw ``stream_cues`` row for ``audio_hash``: ``CUE_ROW`` with ``overrides``."""
    values = {"audio_hash": audio_hash, **CUE_ROW, **overrides}
    query = pg_sql.SQL("INSERT INTO stream_cues ({}) VALUES ({})").format(
        pg_sql.SQL(", ").join(pg_sql.Identifier(name) for name in values),
        pg_sql.SQL(", ").join(pg_sql.Placeholder() for _ in values),
    )
    conn.execute(query, tuple(values.values()))


def cued_file(conn: Conn, **file_fields: Any) -> tuple[UUID, str]:
    """A library file with a fresh ``audio_hash`` and a ``CUE_ROW`` for that audio."""
    hash_text = audio_hash()
    file_id = library_file(conn, audio_hash=hash_text, **file_fields)
    cue_row(conn, hash_text)
    return file_id, hash_text


def cued_master(conn: Conn, **file_fields: Any) -> tuple[UUID, str]:
    """A ``mastered_file`` with a fresh ``audio_hash`` and a ``CUE_ROW`` for that audio."""
    hash_text = audio_hash()
    file_id = mastered_file(conn, audio_hash=hash_text, **file_fields)
    cue_row(conn, hash_text)
    return file_id, hash_text
