"""Rollback 0044 brings the links written since 0044 back as artist rows of ``matches``
(artist model phase 1a, S17; the diff review of PR #214).

Before it drops ``log_name_links`` the rollback brings the old rows up to date, so the code
before 1a finds what it would have written: an active link with no row becomes one (a manual
link tier manual, score 1.0; an automatic one its evidence's tier and score), and a name an
Unmatch put back in review loses its rows, as Unmatch deleted them before 1a. Rows the copy
made from are left as they are. The locked ``test_log_name_links_copy_pg.py::test_rollback``
pins the rest. Everything runs in one transaction, rolled back, so the test database keeps 0044.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.log_name_links import PgLogNameLinkRepository
from backend.domain.enums import LogNameLinkOrigin, LogNameLinkState
from backend.domain.matching import LogNameLink

Conn = psycopg.Connection[Any]

ROLLBACK = (
    Path(__file__).resolve().parents[2] / "backend" / "db" / "rollback_0044_log_name_links.sql"
)
METALLICA = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"


def _name(conn: Conn, spelling: str, status: str) -> UUID:
    name_id = uuid4()
    conn.execute(
        "INSERT INTO broadcast_artists (id, original_name, normalized_name, match_status)"
        " VALUES (%s, %s, %s, %s)",
        (name_id, spelling, spelling.lower(), status),
    )
    return name_id


def _artist(conn: Conn, name: str, mbid: str | None) -> str:
    artist_id = str(uuid4())
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, normalized_name, mbid, origin)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (artist_id, name, name, name.lower(), mbid, "local" if mbid is None else "musicbrainz"),
    )
    return artist_id


def _rows(conn: Conn, name_id: UUID) -> list[tuple[Any, ...]]:
    found = conn.execute(
        "SELECT target_id, target_type, match_tier, confidence_score FROM matches"
        " WHERE artist_id = %s ORDER BY target_id",
        (name_id,),
    ).fetchall()
    return [tuple(row.values()) for row in found]


@pytest.mark.integration
def test_links_since_0044_come_back_as_rows_and_unmatched_names_lose_theirs(
    migrated_db: str,
) -> None:
    conn = psycopg.connect(migrated_db, row_factory=dict_row)
    try:
        links = PgLogNameLinkRepository(conn)
        metallica = _artist(conn, "Metallica", METALLICA)
        local = _artist(conn, "Van Halen", None)
        resolved = _name(conn, "METALLICA", "manual_matched")  # Resolve, since 0044
        matched = _name(conn, "VAN HALEN", "auto_matched")  # the matcher, since 0044
        unmatched = _name(conn, "MEGADETH", "needs_review")  # an Unmatch, since 0044
        copied = _name(conn, "METALLICA (LIVE)", "auto_matched")  # copied by 0044
        links.upsert_link(
            LogNameLink(
                broadcast_artist_id=resolved,
                artist_id=metallica,
                mbid=METALLICA,
                state=LogNameLinkState.ACTIVE,
                origin=LogNameLinkOrigin.MANUAL,
                strength=100,
                evidence={"version": 0, "tier": "manual", "score": 1.0},
            )
        )
        links.upsert_link(
            LogNameLink(
                broadcast_artist_id=matched,
                artist_id=local,
                state=LogNameLinkState.ACTIVE,
                origin=LogNameLinkOrigin.AUTOMATIC,
                strength=88,
                evidence={"version": 0, "tier": "normalization", "score": 87.5},
            )
        )
        links.upsert_link(
            LogNameLink(
                broadcast_artist_id=matched,
                artist_id=metallica,
                state=LogNameLinkState.SUGGESTED,
                origin=LogNameLinkOrigin.AUTOMATIC,
                strength=60,
                evidence={"version": 0},
            )
        )
        for name_id, target in ((unmatched, "mbid-megadeth"), (copied, METALLICA)):
            conn.execute(
                "INSERT INTO matches (artist_id, target_id, target_type, confidence_score,"
                " match_tier) VALUES (%s, %s, 'artist', 100.0, 'normalization')",
                (name_id, target),
            )
        links.upsert_link(
            LogNameLink(
                broadcast_artist_id=copied,
                artist_id=metallica,
                mbid=METALLICA,
                state=LogNameLinkState.ACTIVE,
                origin=LogNameLinkOrigin.LEGACY,
                strength=100,
                evidence={"version": 0, "legacy": {}},
            )
        )

        conn.execute(ROLLBACK.read_text(encoding="utf-8"))

        after = {
            name_id: _rows(conn, name_id) for name_id in (resolved, matched, unmatched, copied)
        }
    finally:
        conn.rollback()
        conn.close()

    assert after[resolved] == [(METALLICA, "artist", "manual", 1.0)]
    assert after[matched] == [(local, "artist", "normalization", 87.5)]  # the suggestion is lost
    assert after[unmatched] == []
    assert after[copied] == [(METALLICA, "artist", "normalization", 100.0)]  # left as it was
