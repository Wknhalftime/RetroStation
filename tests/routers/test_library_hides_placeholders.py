"""The Library's artist list and artist search hide MusicBrainz's special-purpose artists
(artist model phase 1c, SPEC S7; C7).

A catalog artist whose MBID is one of ``SPECIAL_PURPOSE_MBIDS`` (Various Artists, [unknown] and
the rest) is a placeholder, never a performer: ``GET /library/artists`` leaves it out of both
the count and the items, with or without a search. It goes by the MBID, not the name: a local
act named like one ("Various Artists Tribute") and every other artist stay listed. So the
Resolution Center's library search, which reads this list, never offers one either (Resolve
refuses it, S5).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.services.artist_linking_service import SPECIAL_PURPOSE_MBIDS

Conn = psycopg.Connection[Any]

METALLICA = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"
UNKNOWN = "125ec42a-7229-4250-afc5-e057484327fe"


def _artist(conn: Conn, name: str, mbid: str | None) -> str:
    artist_id = str(uuid4())
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, normalized_name, mbid, origin,"
        " needs_enhancement) VALUES (%s, %s, %s, %s, %s, %s, FALSE)",
        (artist_id, name, name, name.lower(), mbid, "local" if mbid is None else "musicbrainz"),
    )
    conn.commit()
    return artist_id


def _listed(client: TestClient, query: str = "") -> tuple[int, set[str]]:
    response = client.get(f"/api/v1/library/artists{query}")
    assert response.status_code == 200
    body = response.json()
    return body["total"], {item["id"] for item in body["items"]}


def test_placeholders_are_left_out_of_the_list_and_search(
    client: TestClient, db_conn: Conn
) -> None:
    metallica = _artist(db_conn, "Metallica", METALLICA)
    tribute = _artist(db_conn, "Various Artists Tribute", None)
    _artist(db_conn, "Various Artists", VARIOUS_ARTISTS)
    _artist(db_conn, "[unknown]", UNKNOWN)

    assert _listed(client) == (2, {metallica, tribute})
    assert _listed(client, "?limit=1")[0] == 2
    assert _listed(client, "?search=various") == (1, {tribute})
    assert _listed(client, "?search=Various%20Artists") == (1, {tribute})
    assert _listed(client, "?search=unknown") == (0, set())
    assert _listed(client, "?search=metal") == (1, {metallica})


@pytest.mark.parametrize("mbid", sorted(SPECIAL_PURPOSE_MBIDS))
def test_every_special_purpose_mbid_is_hidden(client: TestClient, db_conn: Conn, mbid: str) -> None:
    _artist(db_conn, "Placeholder Act", mbid)
    real = _artist(db_conn, "Placeholder Actors", str(uuid4()))

    assert _listed(client) == (1, {real})
    assert _listed(client, "?search=placeholder") == (1, {real})
