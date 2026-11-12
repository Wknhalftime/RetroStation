"""Seeds and reads for Credit picks' tests (performances phase 3a PR 3, SPEC S5-S9).

Each helper writes one thing a test names, by plain SQL, so the tests read the schema as 0051
and 0052 leave it. A line is a log line (``track_identities``) whose log name
(``broadcast_artists``) has links (``log_name_links``) to catalog artists; it is matched, by the
view's rule, to the file of its best match. A performance's credit names artists by id (with
their MBID), or by MBID alone (D1).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from backend.domain.library import ARTIST_CREDITS_BACKFILLED_SETTING, PERFORMANCES_BUILT_SETTING

type Conn = psycopg.Connection[DictRow]

DAY = datetime(2001, 1, 1, 8, tzinfo=UTC)


def _id(row: DictRow | None, column: str = "id") -> UUID:
    assert row is not None
    return UUID(str(row[column]))


# --- the catalog ----------------------------------------------------------------------------------


def artist(conn: Conn, name: str, *, mbid: str | None = None) -> str:
    """A catalog artist named *name*, holding *mbid* (a fresh one unless given)."""
    artist_id = f"artist-{uuid4()}"
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, mbid) VALUES (%s, %s, %s, %s)",
        (artist_id, name, name, mbid if mbid is not None else str(uuid4())),
    )
    return artist_id


def mbid_of(conn: Conn, artist_id: str) -> str:
    row = conn.execute("SELECT mbid FROM artists WHERE id = %s", (artist_id,)).fetchone()
    assert row is not None and row["mbid"] is not None
    return str(row["mbid"])


def merge(conn: Conn, member_id: str, primary_id: str) -> None:
    """2a's Merge: *member_id* is merged into *primary_id*."""
    conn.execute(
        "INSERT INTO artist_merges (artist_id, primary_id) VALUES (%s, %s)",
        (member_id, primary_id),
    )


def work(
    conn: Conn, title: str = "Who Says You Can't Go Home", artist_id: str | None = None
) -> str:
    work_id = f"work-{uuid4()}"
    conn.execute(
        "INSERT INTO works (id, title, artist_id) VALUES (%s, %s, %s)",
        (work_id, title, artist_id or artist(conn, "Bon Jovi")),
    )
    return work_id


def copy(
    conn: Conn,
    work_id: str | None,
    *,
    fmt: str = "flac",
    status: str = "present",
    title: str | None = None,
    recording_mbid: str | None = None,
) -> UUID:
    """A file on *work_id* (its own ``work_id``) in *fmt*; flac ranks above mp3 by the master
    rules."""
    row = conn.execute(
        """INSERT INTO library_files
               (file_path, file_status, duration_ms, work_id, track_title, format, file_size,
                file_mtime_ns, recording_mbid)
           VALUES (%s, %s, 200000, %s, %s, %s, 4000000, 800000000000000000, %s)
           RETURNING id""",
        (f"D:/music/{uuid4()}.{fmt}", status, work_id, title, fmt, recording_mbid),
    ).fetchone()
    return _id(row)


def performance(
    conn: Conn,
    work_id: str,
    files: Sequence[UUID],
    *,
    credit: Sequence[str] = (),
    by_mbid: Sequence[str] = (),
    recording_mbid: str | None = None,
    preferred: UUID | None = None,
) -> UUID:
    """A performance of *work_id* holding *files*, credited to the artists *credit* (by id, with
    their MBIDs) and then to the MBIDs *by_mbid* (no artist, D1), in that order."""
    held = _id(
        conn.execute(
            """INSERT INTO performances (work_id, recording_mbid, version_type, length_ms)
               VALUES (%s, %s, 'original', 200000) RETURNING id""",
            (work_id, recording_mbid),
        ).fetchone()
    )
    position = 0
    for artist_id in credit:
        conn.execute(
            """INSERT INTO performance_credits
                   (performance_id, position, artist_id, mbid, credited_name, role)
               SELECT %s, %s, a.id, a.mbid, a.name, 'main' FROM artists a WHERE a.id = %s""",
            (held, position, artist_id),
        )
        position += 1
    for mbid in by_mbid:
        conn.execute(
            """INSERT INTO performance_credits (performance_id, position, mbid, role)
               VALUES (%s, %s, %s, 'main')""",
            (held, position, mbid),
        )
        position += 1
    for file_id in files:
        conn.execute("UPDATE library_files SET performance_id = %s WHERE id = %s", (held, file_id))
    if preferred is not None:
        conn.execute(
            "UPDATE performances SET preferred_copy_id = %s WHERE id = %s", (preferred, held)
        )
    return held


def master(conn: Conn, work_id: str, file_id: UUID) -> None:
    conn.execute(
        """INSERT INTO song_masters (work_id, preferred_file_id) VALUES (%s, %s)
           ON CONFLICT (work_id) DO UPDATE SET preferred_file_id = EXCLUDED.preferred_file_id""",
        (work_id, file_id),
    )


def version_entry(conn: Conn, work_id: str, text: str, file_id: UUID) -> None:
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


def preferred_of(conn: Conn, performance_id: UUID) -> UUID | None:
    row = conn.execute(
        "SELECT preferred_copy_id FROM performances WHERE id = %s", (performance_id,)
    ).fetchone()
    assert row is not None
    return None if row["preferred_copy_id"] is None else UUID(str(row["preferred_copy_id"]))


# --- log names, lines and plays -------------------------------------------------------------------


def log_name(conn: Conn, name: str, *artists: str, state: str = "active") -> UUID:
    """The log name *name*, linked to each of *artists* in *state*."""
    name_id = _id(
        conn.execute(
            """INSERT INTO broadcast_artists (original_name, normalized_name)
               VALUES (%s, %s) RETURNING id""",
            (name, f"{name.lower()} {uuid4().hex}"),
        ).fetchone()
    )
    for artist_id in artists:
        link(conn, name_id, artist_id, state=state)
    return name_id


def link(conn: Conn, name_id: UUID, artist_id: str, *, state: str = "active") -> None:
    conn.execute(
        """INSERT INTO log_name_links
               (broadcast_artist_id, artist_id, mbid, state, origin, strength, evidence)
           SELECT %s, a.id, a.mbid, %s, 'manual', 100, %s FROM artists a WHERE a.id = %s""",
        (name_id, state, Jsonb({"version": 1}), artist_id),
    )


def line(
    conn: Conn,
    title: str,
    file_id: UUID | None,
    *,
    name: UUID | None = None,
    status: str = "auto_matched",
    confidence: float = 0.9,
) -> UUID:
    """A log line titled *title* under the log name *name* (a fresh unlinked one unless given),
    its one match on *file_id* (none when None)."""
    name_id = name if name is not None else log_name(conn, "Somebody")
    identity_id = _id(
        conn.execute(
            """INSERT INTO track_identities
                   (broadcast_artist_id, original_title, normalized_title, normalized_signature,
                    match_status)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (name_id, title, title.lower(), uuid4().hex, status),
        ).fetchone()
    )
    if file_id is not None:
        also_match(conn, identity_id, file_id, confidence=confidence)
    return identity_id


def also_match(conn: Conn, identity_id: UUID, file_id: UUID, *, confidence: float) -> UUID:
    """Another match of the line, on *file_id*."""
    return _id(
        conn.execute(
            """INSERT INTO matches (identity_id, library_file_id, confidence_score)
               VALUES (%s, %s, %s) RETURNING id""",
            (identity_id, file_id, confidence),
        ).fetchone()
    )


def best_match(conn: Conn, identity_id: UUID) -> UUID:
    """The line's best match by the view's rule."""
    return _id(
        conn.execute(
            """SELECT id FROM matches
               WHERE identity_id = %s AND library_file_id IS NOT NULL
               ORDER BY confidence_score DESC, created_at, id LIMIT 1""",
            (identity_id,),
        ).fetchone()
    )


def store(conn: Conn, identity_id: UUID, performance_id: UUID) -> None:
    """Seed a stored performance on the line's best match, as a pick would leave it."""
    conn.execute(
        "UPDATE matches SET performance_id = %s WHERE id = %s",
        (performance_id, best_match(conn, identity_id)),
    )


def stored(conn: Conn, identity_id: UUID) -> set[UUID | None]:
    """The performance each of the line's matches stores."""
    rows = conn.execute(
        "SELECT performance_id FROM matches WHERE identity_id = %s", (identity_id,)
    ).fetchall()
    return {None if r["performance_id"] is None else UUID(str(r["performance_id"])) for r in rows}


def station(conn: Conn, call_letters: str = "KISW", format_name: str = "AOR") -> UUID:
    """The station *call_letters* (unique), created on first use."""
    return _id(
        conn.execute(
            """INSERT INTO stations (call_letters, format_name) VALUES (%s, %s)
               ON CONFLICT (call_letters) DO UPDATE SET call_letters = EXCLUDED.call_letters
               RETURNING id""",
            (call_letters, format_name),
        ).fetchone()
    )


def plays(conn: Conn, identity_id: UUID, count: int = 1, *, station_id: UUID | None = None) -> None:
    """*count* plays of the line, on consecutive days."""
    at = station_id if station_id is not None else station(conn)
    playlist = conn.execute(
        """INSERT INTO playlists (name, content_hash, station_id)
           VALUES (%s, %s, %s) RETURNING id""",
        (f"log-{uuid4()}.csv", uuid4().hex, at),
    ).fetchone()
    assert playlist is not None
    for n in range(count):
        conn.execute(
            "INSERT INTO play_events (identity_id, playlist_id, played_at) VALUES (%s, %s, %s)",
            (identity_id, playlist["id"], DAY.replace(day=1 + n)),
        )


def resolved(conn: Conn, identity_id: UUID) -> set[tuple[UUID | None, str | None]]:
    """(file_id, source) of each play of the line, as the air-play view resolves it."""
    rows = conn.execute(
        """SELECT pfr.file_id, pfr.source FROM play_file_resolution pfr
           JOIN play_events pe ON pe.id = pfr.play_event_id
           WHERE pe.identity_id = %s""",
        (identity_id,),
    ).fetchall()
    return {(None if r["file_id"] is None else UUID(str(r["file_id"])), r["source"]) for r in rows}


def every_play(conn: Conn) -> set[tuple[Any, ...]]:
    """Every play's row of the air-play view."""
    rows = conn.execute(
        """SELECT pfr.play_event_id, pfr.file_id, pfr.file_status, pfr.source
           FROM play_events pe
           CROSS JOIN LATERAL (
               SELECT * FROM play_file_resolution r WHERE r.play_event_id = pe.id OFFSET 0
           ) pfr"""
    ).fetchall()
    return {tuple(r.values()) for r in rows}


# --- markers and proposals ------------------------------------------------------------------------


def set_marker(conn: Conn, key: str) -> None:
    conn.execute(
        """INSERT INTO user_settings (key, value) VALUES (%s, '2026-11-12T00:00:00+00:00')
           ON CONFLICT (key) DO NOTHING""",
        (key,),
    )


def mark_built(conn: Conn) -> None:
    """2a's backfill and PR 2's build both count as done."""
    set_marker(conn, ARTIST_CREDITS_BACKFILLED_SETTING)
    set_marker(conn, PERFORMANCES_BUILT_SETTING)


def proposals(conn: Conn) -> list[dict[str, Any]]:
    """Every row of ``credit_pick_proposals``, by line."""
    return list(
        conn.execute(
            """SELECT id, identity_id, match_id, performance_id, kind, reason, candidates,
                      state, decided_at
               FROM credit_pick_proposals ORDER BY identity_id"""
        ).fetchall()
    )


def proposal_of(conn: Conn, identity_id: UUID) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT id, identity_id, match_id, performance_id, kind, reason, candidates, state,
                  decided_at
           FROM credit_pick_proposals WHERE identity_id = %s""",
        (identity_id,),
    ).fetchone()
    return None if row is None else dict(row)


def propose(
    conn: Conn,
    identity_id: UUID,
    performance_id: UUID | None,
    *,
    kind: str = "credit",
    reason: str = "credit",
    candidates: Sequence[UUID] | None = None,
    state: str = "proposed",
) -> UUID:
    """Seed a proposal as the task would write it, on the line's best match."""
    listed = list(candidates) if candidates is not None else [performance_id]
    return _id(
        conn.execute(
            """INSERT INTO credit_pick_proposals
                   (identity_id, match_id, performance_id, kind, reason, candidates, state,
                    decided_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s,
                       CASE WHEN %s::text = 'proposed' THEN NULL ELSE now() END)
               RETURNING id""",
            (
                identity_id,
                best_match(conn, identity_id),
                performance_id,
                kind,
                reason,
                listed,
                state,
                state,
            ),
        ).fetchone()
    )


class Duet:
    """Bon Jovi's "Who Says You Can't Go Home": the solo master (flac) and its performance, and
    the duet with Jennifer Nettles, a flac and an mp3 copy, with no preferred copy yet. The log
    names "BON JOVI" and "BON JOVI/JENNIFER NETTLES" are linked to their artists."""

    def __init__(self, conn: Conn) -> None:
        self.bon_jovi = artist(conn, "Bon Jovi")
        self.nettles = artist(conn, "Jennifer Nettles")
        self.work = work(conn, "Who Says You Can't Go Home", self.bon_jovi)
        self.master = copy(conn, self.work, title="Who Says You Can't Go Home")
        self.solo = performance(conn, self.work, [self.master], credit=[self.bon_jovi])
        master(conn, self.work, self.master)
        self.duet_mp3 = copy(conn, self.work, fmt="mp3", title="Who Says You Can't Go Home")
        self.duet_flac = copy(conn, self.work, fmt="flac", title="Who Says You Can't Go Home")
        self.duet = performance(
            conn, self.work, [self.duet_mp3, self.duet_flac], credit=[self.bon_jovi, self.nettles]
        )
        self.solo_name = log_name(conn, "BON JOVI", self.bon_jovi)
        self.duet_name = log_name(conn, "BON JOVI/JENNIFER NETTLES", self.bon_jovi, self.nettles)

    def duet_line(self, conn: Conn, title: str = "Who Says You Can't Go Home") -> UUID:
        """A duet line matched to the solo master, with one play."""
        identity_id = line(conn, title, self.master, name=self.duet_name)
        plays(conn, identity_id)
        return identity_id

    def solo_line(self, conn: Conn, title: str = "Who Says You Can't Go Home") -> UUID:
        identity_id = line(conn, title, self.master, name=self.solo_name)
        plays(conn, identity_id)
        return identity_id
