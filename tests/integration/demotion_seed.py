"""Seeds for migration 0045's tests (artist model phase 1c, SPEC S1 to S3): the database as it
was before 0045, and log names and links as 0044 and 1a's writers left them.

Locked with the tests that use it (``test_link_demotion_pg.py``, ``test_status_from_links_pg.py``),
as 1b's ``tests/routers/cascade_seed.py`` is.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

Conn = psycopg.Connection[Any]

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0045_link_demotion"
MIGRATION = ROOT / "backend" / "db" / "migrations" / f"{VERSION}.sql"
ROLLBACK = ROOT / "backend" / "db" / f"rollback_{VERSION}.sql"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@contextmanager
def before_the_demotion(url: str) -> Iterator[Conn]:
    """The test database without 0045, in one transaction that is rolled back at the end, so the
    test database keeps 0045."""
    conn = psycopg.connect(url, row_factory=dict_row)
    try:
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        yield conn
    finally:
        conn.rollback()
        conn.close()


def migrate(conn: Conn) -> None:
    """Run 0045's SQL on *conn*, as the runner does (inside the caller's transaction)."""
    conn.execute(MIGRATION.read_text(encoding="utf-8"))


def roll_back(conn: Conn) -> None:
    conn.execute(ROLLBACK.read_text(encoding="utf-8"))


def log_name(
    conn: Conn,
    spelling: str,
    status: str = "auto_matched",
    reason: str | None = None,
    detail: str | None = None,
) -> UUID:
    """A log name; its normalized name is the spelling in lower case."""
    name_id = uuid4()
    conn.execute(
        "INSERT INTO broadcast_artists"
        " (id, original_name, normalized_name, match_status, reason_code, reason_detail)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (name_id, spelling, spelling.lower(), status, reason, detail),
    )
    return name_id


def catalog_artist(conn: Conn, name: str, mbid: str | None = None) -> str:
    """A catalog artist: MusicBrainz with an MBID, else local. Its normalized name is left
    NULL, so two artists may share a name."""
    artist_id = str(uuid4())
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, normalized_name, mbid, origin,"
        " needs_enhancement) VALUES (%s, %s, %s, NULL, %s, %s, FALSE)",
        (artist_id, name, name, mbid, "musicbrainz" if mbid else "local"),
    )
    return artist_id


def strength(score: float) -> int:
    """A score as a link's strength: rounded half up, as 0044 and the link service do."""
    return max(0, min(100, math.floor(score + 0.5)))


def link(
    conn: Conn,
    name_id: UUID,
    artist_id: str,
    *,
    tier: str = "normalization",
    score: float = 100.0,
    state: str = "active",
    origin: str = "legacy",
    at: datetime = T0,
) -> UUID:
    """A link as 0044 copied it (origin ``legacy``, the old row in its v0 evidence), as the
    matcher rewrote it since 1a (``automatic``, keeping the copied row) or as Resolve wrote it
    (``manual``)."""
    legacy = {
        "match_id": str(uuid4()),
        "target_id": artist_id,
        "tier": tier,
        "score": score,
        "created_at": at.isoformat(),
    }
    evidence: dict[str, object]
    if origin == "manual":
        evidence, held = {"version": 0, "tier": "manual", "score": 1.0}, 100
    elif origin == "automatic":
        evidence = {"version": 0, "tier": tier, "score": score, "legacy": legacy}
        held = strength(score)
    else:
        evidence = {"version": 0, "legacy": legacy}
        held = 100 if tier == "manual" else strength(score)
    link_id = uuid4()
    conn.execute(
        """INSERT INTO log_name_links
               (id, broadcast_artist_id, artist_id, state, origin, strength, evidence,
                created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (link_id, name_id, artist_id, state, origin, held, json.dumps(evidence), at, at),
    )
    return link_id
