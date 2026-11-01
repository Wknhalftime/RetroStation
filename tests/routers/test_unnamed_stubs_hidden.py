"""An unnamed stub artist is hidden from the Library's artist list and artist search (artist
model phase 1a, SPEC S15; S3).

A stub is the catalog artist 1a makes for an MBID no artist holds: its name is the MBID until
enrichment names it (phase 1a PR 2). Until then ``GET /library/artists`` leaves it out of both
the count and the items, with or without a search, using ``name IS DISTINCT FROM mbid`` (a
local artist's ``mbid`` is NULL, and it stays listed). A named MusicBrainz artist stays listed.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

Conn = psycopg.Connection[Any]

METALLICA = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
UNNAMED = "00000000-0000-4000-8000-0000000000aa"


def _artist(conn: Conn, name: str, mbid: str | None, normalized: str | None) -> str:
    artist_id = str(uuid4())
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, normalized_name, mbid, origin,"
        " needs_enhancement) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            artist_id,
            name,
            name,
            normalized,
            mbid,
            "local" if mbid is None else "musicbrainz",
            mbid is not None,
        ),
    )
    conn.commit()
    return artist_id


def _listed(client: TestClient, query: str = "") -> tuple[int, set[str]]:
    response = client.get(f"/api/v1/library/artists{query}")
    assert response.status_code == 200
    body = response.json()
    return body["total"], {item["id"] for item in body["items"]}


def test_unnamed_stubs_are_left_out_of_the_list_and_search(
    client: TestClient, db_conn: Conn
) -> None:
    local = _artist(db_conn, "Van Halen", None, "van halen")
    named = _artist(db_conn, "Metallica", METALLICA, "metallica")
    _artist(db_conn, UNNAMED, UNNAMED, None)

    assert _listed(client) == (2, {local, named})
    assert _listed(client, "?limit=1")[0] == 2
    assert _listed(client, f"?search={UNNAMED}") == (0, set())
    assert _listed(client, f"?search={UNNAMED[:8]}") == (0, set())
    assert _listed(client, "?search=metal") == (1, {named})
    assert _listed(client, "?search=van") == (1, {local})


def test_a_named_stub_is_listed(client: TestClient, db_conn: Conn) -> None:
    """Once enrichment names it, the stub is an ordinary MusicBrainz artist."""
    named = _artist(db_conn, "Ozzy Osbourne", UNNAMED, None)

    assert _listed(client) == (1, {named})
    assert _listed(client, "?search=ozzy") == (1, {named})
