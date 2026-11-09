"""Seeds and reads for the version pages' route tests (versions phase 2b PR 3, SPEC S5-S8, S11,
S12).

Each helper writes one thing a test names. A line's version text comes from its title (the
generated ``track_identities.version_text``, C59): "Enter Sandman (Live S&M)" is "live s&m". A
line is matched, by the view's rule, to the file of its best match.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import DictRow

type Conn = psycopg.Connection[DictRow]

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
"""A state's seen_at. Files are indexed before it unless a test says otherwise."""
BEFORE = T0 - timedelta(days=30)
AFTER = T0 + timedelta(days=1)


def artist(conn: Conn, name: str = "Metallica") -> str:
    artist_id = f"artist-{uuid4()}"
    conn.execute(
        "INSERT INTO artists (id, name, sort_name) VALUES (%s, %s, %s)",
        (artist_id, name, name),
    )
    return artist_id


def merge(conn: Conn, member_id: str, primary_id: str) -> None:
    """2a's Merge: ``member_id`` is merged into ``primary_id``."""
    conn.execute(
        "INSERT INTO artist_merges (artist_id, primary_id) VALUES (%s, %s)",
        (member_id, primary_id),
    )


def work(conn: Conn, title: str = "Enter Sandman", artist_id: str | None = None) -> str:
    work_id = f"work-{uuid4()}"
    conn.execute(
        "INSERT INTO works (id, title, artist_id) VALUES (%s, %s, %s)",
        (work_id, title, artist_id or artist(conn)),
    )
    return work_id


def recording(conn: Conn, work_id: str, version_type: str = "original") -> str:
    """The work's recording of *version_type*, created on first use (one per work and type)."""
    row = conn.execute(
        "SELECT id FROM recordings WHERE work_id = %s AND version_type = %s",
        (work_id, version_type),
    ).fetchone()
    if row is not None:
        return str(row["id"])
    recording_id = f"rec-{uuid4()}"
    conn.execute(
        "INSERT INTO recordings (id, title, work_id, version_type) VALUES (%s, %s, %s, %s)",
        (recording_id, "Recording", work_id, version_type),
    )
    return recording_id


def file(
    conn: Conn,
    work_id: str | None,
    *,
    version_type: str = "original",
    title: str | None = None,
    status: str = "present",
    indexed_at: datetime = BEFORE,
    recording_work: str | None = None,
) -> UUID:
    """A file whose own ``work_id`` is *work_id*, of the recording of *version_type* on
    *recording_work* (default: *work_id*; none when both are None)."""
    on = recording_work or work_id
    recording_id = None if on is None else recording(conn, on, version_type)
    row = conn.execute(
        """INSERT INTO library_files
               (file_path, file_status, duration_ms, recording_id, work_id, track_title,
                format, file_size, file_mtime_ns, indexed_at)
           VALUES (%s, %s, 200000, %s, %s, %s, 'flac', 4000000, 800000000000000000, %s)
           RETURNING id""",
        (f"D:/music/{uuid4()}.flac", status, recording_id, work_id, title, indexed_at),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def path_of(conn: Conn, file_id: UUID) -> str:
    row = conn.execute("SELECT file_path FROM library_files WHERE id = %s", (file_id,)).fetchone()
    assert row is not None
    return str(row["file_path"])


def master(conn: Conn, work_id: str, file_id: UUID) -> None:
    conn.execute(
        "INSERT INTO song_masters (work_id, preferred_file_id) VALUES (%s, %s)",
        (work_id, file_id),
    )


def station(conn: Conn, call_letters: str, format_name: str = "AOR") -> UUID:
    """The station *call_letters* (unique), created on first use."""
    row = conn.execute(
        """INSERT INTO stations (call_letters, format_name) VALUES (%s, %s)
           ON CONFLICT (call_letters) DO UPDATE SET call_letters = EXCLUDED.call_letters
           RETURNING id""",
        (call_letters, format_name),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def line(
    conn: Conn,
    title: str,
    file_id: UUID | None,
    *,
    status: str = "auto_matched",
    confidence: float = 0.9,
) -> UUID:
    """A log line titled *title*, its one match on *file_id* (none when None)."""
    artist_row = conn.execute(
        """INSERT INTO broadcast_artists (original_name, normalized_name)
           VALUES ('Artist', %s) RETURNING id""",
        (f"artist {uuid4().hex}",),
    ).fetchone()
    assert artist_row is not None
    row = conn.execute(
        """INSERT INTO track_identities
               (broadcast_artist_id, original_title, normalized_title, normalized_signature,
                match_status)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (artist_row["id"], title, title.lower(), uuid4().hex, status),
    ).fetchone()
    assert row is not None
    identity_id = UUID(str(row["id"]))
    if file_id is not None:
        also_match(conn, identity_id, file_id, confidence=confidence)
    return identity_id


def also_match(conn: Conn, identity_id: UUID, file_id: UUID, *, confidence: float) -> None:
    """Another match of the line, on *file_id*."""
    conn.execute(
        """INSERT INTO matches (identity_id, library_file_id, confidence_score)
           VALUES (%s, %s, %s)""",
        (identity_id, file_id, confidence),
    )


def plays(conn: Conn, identity_id: UUID, station_id: UUID, *times: datetime) -> None:
    """One play of the line at *station_id* for each of *times* (``played_at``, wall clock)."""
    playlist = conn.execute(
        """INSERT INTO playlists (name, content_hash, station_id)
           VALUES (%s, %s, %s) RETURNING id""",
        (f"log-{uuid4()}.csv", uuid4().hex, station_id),
    ).fetchone()
    assert playlist is not None
    for played_at in times:
        conn.execute(
            "INSERT INTO play_events (identity_id, playlist_id, played_at) VALUES (%s, %s, %s)",
            (identity_id, playlist["id"], played_at),
        )


def entry(conn: Conn, work_id: str, text: str, file_id: UUID) -> None:
    conn.execute(
        """INSERT INTO format_overrides (work_id, version_text, preferred_file_id)
           VALUES (%s, %s, %s)""",
        (work_id, text, file_id),
    )


def override(conn: Conn, work_id: str, format_name: str, file_id: UUID) -> None:
    conn.execute(
        """INSERT INTO format_overrides (work_id, format_name, preferred_file_id)
           VALUES (%s, %s, %s)""",
        (work_id, format_name, file_id),
    )


def state(
    conn: Conn, work_id: str, text: str, kind: str | None = None, seen_at: datetime = T0
) -> None:
    conn.execute(
        """INSERT INTO version_entry_states (work_id, version_text, state, seen_at)
           VALUES (%s, %s, %s, %s)""",
        (work_id, text, kind, seen_at),
    )


def entries_of(conn: Conn, work_id: str) -> dict[str, UUID]:
    """The work's version entries: text -> file."""
    rows = conn.execute(
        """SELECT version_text, preferred_file_id FROM format_overrides
           WHERE work_id = %s AND version_text IS NOT NULL""",
        (work_id,),
    ).fetchall()
    return {r["version_text"]: UUID(str(r["preferred_file_id"])) for r in rows}


def states_of(conn: Conn, work_id: str) -> dict[str, tuple[str | None, datetime]]:
    rows = conn.execute(
        "SELECT version_text, state, seen_at FROM version_entry_states WHERE work_id = %s",
        (work_id,),
    ).fetchall()
    return {r["version_text"]: (r["state"], r["seen_at"]) for r in rows}


def all_states(conn: Conn) -> set[tuple[str, str, str | None]]:
    rows = conn.execute("SELECT work_id, version_text, state FROM version_entry_states").fetchall()
    return {(r["work_id"], r["version_text"], r["state"]) for r in rows}


def resolved(conn: Conn, identity_id: UUID) -> set[tuple[UUID | None, str | None]]:
    """(file_id, source) of each play of the line, as the air-play view resolves it."""
    rows = conn.execute(
        """SELECT pfr.file_id, pfr.source FROM play_file_resolution pfr
           JOIN play_events pe ON pe.id = pfr.play_event_id
           WHERE pe.identity_id = %s""",
        (identity_id,),
    ).fetchall()
    return {(None if r["file_id"] is None else UUID(str(r["file_id"])), r["source"]) for r in rows}


def now(conn: Conn) -> datetime:
    """The database's clock, for comparing a seen_at a route wrote with now()."""
    row = conn.execute("SELECT now() AS t").fetchone()
    assert row is not None
    value: datetime = row["t"]
    return value
