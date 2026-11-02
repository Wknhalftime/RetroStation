"""Seeds and reads for the phase 1b cascade route tests (SPEC S2, S3, S5, S7; E1, E2, E3, E5).

A log name and its lines on PostgreSQL, through plain SQL, so each test states exactly which
line is matched to which file. A line "matched to a same-name file" has a match row whose
library file's ``normalized_artist_name`` is the name's ``normalized_name`` (AUD-R022 D9, C16);
both are made with ``normalize_artist``, as the scan and the ingest make them. Each line's plays
are play events on one playlist of one station, so ``station_identity_plays`` counts them too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg

from backend.domain.enums import LogNameLinkOrigin, LogNameLinkState, MatchStatus
from backend.services.normalization import normalize_artist
from tests.matching_seed import seed_pg_link

Conn = psycopg.Connection[Any]

ON_AIR = ("auto_matched", "manual_matched")


@dataclass(frozen=True)
class Line:
    id: UUID
    plays: int
    file_id: UUID | None


class CascadeWorld:
    """One station, one playlist, and the names and lines a test seeds on it."""

    def __init__(self, conn: Conn, call_letters: str = "KCSC") -> None:
        self.conn = conn
        self.station_id = uuid4()
        self.playlist_id = uuid4()
        conn.execute(
            "INSERT INTO stations (id, call_letters) VALUES (%s, %s)",
            (self.station_id, call_letters),
        )
        conn.execute(
            "INSERT INTO playlists (id, name, content_hash, station_id) VALUES (%s, %s, %s, %s)",
            (self.playlist_id, "show.csv", uuid4().hex, self.station_id),
        )
        conn.commit()
        self._clock = datetime(1995, 3, 14, 8, 0, tzinfo=UTC)

    def name(
        self, spelling: str = "CASCADE BAND", status: MatchStatus = MatchStatus.AUTO_MATCHED
    ) -> UUID:
        name_id = uuid4()
        self.conn.execute(
            """INSERT INTO broadcast_artists (id, original_name, normalized_name, match_status)
               VALUES (%s, %s, %s, %s)""",
            (name_id, spelling, normalize_artist(spelling), status.value),
        )
        self.conn.commit()
        return name_id

    def link(
        self,
        name_id: UUID,
        target: str,
        *,
        origin: LogNameLinkOrigin = LogNameLinkOrigin.AUTOMATIC,
        state: LogNameLinkState = LogNameLinkState.ACTIVE,
        strength: int = 90,
    ) -> str:
        """Link the name to the artist *target* names (an MBID gets a stub); the artist's id."""
        link = seed_pg_link(
            self.conn, name_id, target, origin=origin, state=state, strength=strength
        )
        self.conn.commit()
        return link.artist_id

    def line(
        self,
        name_id: UUID,
        title: str,
        status: MatchStatus,
        *,
        file_artist: str | None = None,
        plays: int = 1,
        tier: str | None = "local_file_fuzzy",
        reason_code: str | None = None,
        reason_detail: str | None = None,
        score: float = 0.9,
    ) -> Line:
        """A line of the name with *plays* plays; with *file_artist*, a match to a file tagged
        with that artist (the name's own spelling makes it a same-name file)."""
        line_id = uuid4()
        self.conn.execute(
            """INSERT INTO track_identities
                   (id, broadcast_artist_id, original_title, normalized_title,
                    normalized_signature, match_status, match_tier, reason_code, reason_detail)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                line_id,
                name_id,
                title,
                title.lower(),
                f"{name_id}:{title.lower()}:{line_id.hex}",
                status.value,
                tier,
                reason_code,
                reason_detail,
            ),
        )
        file_id = None if file_artist is None else self.match(line_id, file_artist, score=score)
        for _ in range(plays):
            self._clock += timedelta(minutes=4)
            self.conn.execute(
                """INSERT INTO play_events (id, identity_id, playlist_id, played_at)
                   VALUES (%s, %s, %s, %s)""",
                (uuid4(), line_id, self.playlist_id, self._clock),
            )
        self.conn.commit()
        return Line(id=line_id, plays=plays, file_id=file_id)

    def match(self, line_id: UUID, file_artist: str, *, score: float = 0.9) -> UUID:
        """A match row from the line to a new file tagged with *file_artist*; the file's id."""
        file_id = uuid4()
        self.conn.execute(
            """INSERT INTO library_files
                   (id, file_path, format, enrichment_status, artist_name,
                    normalized_artist_name, track_title)
               VALUES (%s, %s, 'flac', 'pending', %s, %s, 'Song')""",
            (file_id, f"/music/{file_id.hex}.flac", file_artist, normalize_artist(file_artist)),
        )
        self.conn.execute(
            """INSERT INTO matches
                   (id, identity_id, library_file_id, target_type, confidence_score, match_tier)
               VALUES (%s, %s, %s, 'library_file', %s, 'local_file_fuzzy')""",
            (uuid4(), line_id, file_id, score),
        )
        self.conn.commit()
        return file_id


# --- reads ---------------------------------------------------------------------------------------


def line_row(conn: Conn, line_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        """SELECT match_status, match_tier, reason_code, reason_detail
             FROM track_identities WHERE id = %s""",
        (line_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


def name_row(conn: Conn, name_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        "SELECT match_status, reason_code, reason_detail FROM broadcast_artists WHERE id = %s",
        (name_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


def match_files(conn: Conn, line_id: UUID) -> list[UUID | None]:
    rows = conn.execute(
        "SELECT library_file_id FROM matches WHERE identity_id = %s ORDER BY created_at, id",
        (line_id,),
    ).fetchall()
    return [row["library_file_id"] for row in rows]


def links(conn: Conn, name_id: UUID) -> dict[str, dict[str, Any]]:
    """The name's links by artist id."""
    rows = conn.execute(
        "SELECT * FROM log_name_links WHERE broadcast_artist_id = %s", (name_id,)
    ).fetchall()
    return {str(row["artist_id"]): dict(row) for row in rows}


def on_air(conn: Conn, name_id: UUID) -> dict[UUID, tuple[str, int]]:
    """The name's lines on air (matched, with a match to a file), with status and plays."""
    rows = conn.execute(
        """SELECT ti.id, ti.match_status,
                  (SELECT count(*) FROM play_events pe WHERE pe.identity_id = ti.id) AS plays
             FROM track_identities ti
            WHERE ti.broadcast_artist_id = %s
              AND ti.match_status = ANY(%s)
              AND EXISTS (SELECT 1 FROM matches m
                           WHERE m.identity_id = ti.id AND m.library_file_id IS NOT NULL)""",
        (name_id, list(ON_AIR)),
    ).fetchall()
    return {row["id"]: (row["match_status"], int(row["plays"])) for row in rows}


def cascade_body(off_lines: int, off_plays: int, manual: int, same_name: int) -> dict[str, Any]:
    """The preview's and the actions' count body (S5, E2)."""
    return {
        "off_air": {"lines": off_lines, "plays": off_plays},
        "kept": {"manual": manual, "same_name": same_name},
    }
