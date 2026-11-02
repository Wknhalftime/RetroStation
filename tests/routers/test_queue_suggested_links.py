"""The Resolution Center's candidates are the name's suggested links (artist model phase 1c, SPEC
S8; C30, C34, C35).

``broadcast_artists.artist_candidates`` was never written (phase 1c retires it, C34), so the
panel always said "No candidates found automatically". From 1c a queue item's ``candidates`` are
its name's ``suggested`` links, best first (strength, then the oldest), each with the catalog
artist's id, its MBID, name and disambiguation, the link's strength as its score, and a reason
in words. Active and rejected links are not candidates. The backend model types them, so the
OpenAPI schema says what the frontend's zod schema parses. Accepting one is the manual resolve
the panel already does, by the artist's id: the link becomes active and manual, the name
``manual_matched``, its other suggestions stay.

A name with a suggestion is likely, so the queue shows it by default
(``include_unlikely=false``, what the Resolution Center asks for): the demotion puts about a
thousand names back in review with a suggestion each, and a change may widen the curator's
review, never hide it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
from fastapi.testclient import TestClient

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
QUEUE = "/api/v1/matching/queue"
CANDIDATE_FIELDS = {"artist_id", "mbid", "name", "disambiguation", "score", "reason"}


def _name(conn: psycopg.Connection[Any], spelling: str, status: str = "needs_review") -> UUID:
    name_id = uuid4()
    conn.execute(
        "INSERT INTO broadcast_artists (id, original_name, normalized_name, match_status,"
        " reason_code) VALUES (%s, %s, %s, %s, %s)",
        (
            name_id,
            spelling,
            spelling.lower(),
            status,
            "LINK_SUGGESTED" if status == "needs_review" else None,
        ),
    )
    return name_id


def _artist(
    conn: psycopg.Connection[Any],
    name: str,
    mbid: str | None,
    disambiguation: str | None = None,
) -> str:
    artist_id = str(uuid4())
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, mbid, disambiguation, origin,"
        " needs_enhancement) VALUES (%s, %s, %s, %s, %s, %s, FALSE)",
        (artist_id, name, name, mbid, disambiguation, "musicbrainz" if mbid else "local"),
    )
    return artist_id


def _link(
    conn: psycopg.Connection[Any],
    name_id: UUID,
    artist_id: str,
    strength: int,
    *,
    state: str = "suggested",
    origin: str = "automatic",
    at: datetime = T0,
) -> None:
    """A suggestion as the matcher writes one, or (origin legacy) as 0045 demotes one."""
    if origin == "legacy":
        evidence: dict[str, object] = {
            "version": 0,
            "legacy": {"tier": "normalization", "score": float(strength), "target_id": artist_id},
        }
    else:
        evidence = {"version": 0, "tier": "normalization", "score": float(strength)}
    conn.execute(
        """INSERT INTO log_name_links
               (broadcast_artist_id, artist_id, mbid, state, origin, strength, evidence,
                created_at)
           SELECT %s, a.id, a.mbid, %s, %s, %s, %s, %s FROM artists a WHERE a.id = %s""",
        (name_id, state, origin, strength, json.dumps(evidence), at, artist_id),
    )


def _item(client: TestClient, name_id: UUID) -> dict[str, Any]:
    resp = client.get(QUEUE)
    assert resp.status_code == 200
    [item] = [i for i in resp.json()["items"] if i["id"] == str(name_id)]
    return item


def test_candidates_are_the_suggested_links_best_first(
    client: TestClient, db_conn: psycopg.Connection[Any]
) -> None:
    name_id = _name(db_conn, "BLUE OYSTER CULT")
    club = _artist(db_conn, "Blue Oyster Club", "mbid-club", "Long Island bar band")
    local = _artist(db_conn, "Blue Oyster", None)  # a local artist: no MBID
    bluesy = _artist(db_conn, "Bluesy Oysters", "mbid-bluesy")
    tied = _artist(db_conn, "Blue Oysters", "mbid-tied")
    rejected = _artist(db_conn, "Blue Öyster Cult", "mbid-boc")
    _link(db_conn, name_id, bluesy, 73, at=T0)
    _link(db_conn, name_id, club, 88, origin="legacy", at=T0 + timedelta(hours=2))
    _link(db_conn, name_id, tied, 81, at=T0 + timedelta(hours=1))
    _link(db_conn, name_id, local, 81, at=T0)  # same score, older: first of the two
    _link(db_conn, name_id, rejected, 100, state="rejected", origin="manual")
    db_conn.commit()

    candidates = _item(client, name_id)["candidates"]

    assert all(set(c) >= CANDIDATE_FIELDS for c in candidates)
    assert len(candidates) == 4
    assert [
        (c["artist_id"], c["mbid"], c["name"], c["disambiguation"], c["score"]) for c in candidates
    ] == [
        (club, "mbid-club", "Blue Oyster Club", "Long Island bar band", 88),
        (local, None, "Blue Oyster", None, 81),
        (tied, "mbid-tied", "Blue Oysters", None, 81),
        (bluesy, "mbid-bluesy", "Bluesy Oysters", None, 73),
    ]
    assert all(isinstance(c["reason"], str) and c["reason"] for c in candidates)


def test_active_links_are_not_candidates(
    client: TestClient, db_conn: psycopg.Connection[Any]
) -> None:
    """A matched name in the queue through a song under review: its active link is the match,
    not a candidate."""
    name_id = _name(db_conn, "METALLICA", "auto_matched")
    _link(db_conn, name_id, _artist(db_conn, "Metallica", "mbid-m"), 100, state="active")
    song_id = uuid4()
    db_conn.execute(
        """INSERT INTO track_identities
               (id, broadcast_artist_id, original_title, normalized_title, normalized_signature,
                match_status)
           VALUES (%s, %s, 'One', 'one', %s, 'needs_review')""",
        (song_id, name_id, f"metallica:one:{song_id.hex}"),
    )
    db_conn.commit()

    assert _item(client, name_id)["candidates"] == []


def test_a_name_without_suggestions_has_no_candidates(
    client: TestClient, db_conn: psycopg.Connection[Any]
) -> None:
    name_id = _name(db_conn, "ZZZYX QWERTY")
    db_conn.commit()

    assert _item(client, name_id)["candidates"] == []


def test_the_model_types_the_candidates(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    components = schema["components"]["schemas"]
    ref = components["QueueArtist"]["properties"]["candidates"]["items"]["$ref"]
    candidate = components[ref.rsplit("/", 1)[1]]

    assert set(candidate["properties"]) >= CANDIDATE_FIELDS
    assert {"artist_id", "name", "score", "reason"} <= set(candidate.get("required", []))


def test_accepting_a_suggestion_is_the_manual_resolve(
    client: TestClient, db_conn: psycopg.Connection[Any]
) -> None:
    name_id = _name(db_conn, "ARCHIES")
    archies = _artist(db_conn, "Archies", "mbid-archies")
    other = _artist(db_conn, "Architects", "mbid-architects")
    _link(db_conn, name_id, archies, 72, origin="legacy")
    _link(db_conn, name_id, other, 61)
    db_conn.commit()
    [accepted] = [c for c in _item(client, name_id)["candidates"] if c["artist_id"] == archies]

    resp = client.post(
        f"/api/v1/matching/artists/{name_id}/resolve",
        json={"match_status": "manual_matched", "target_artist_id": accepted["artist_id"]},
    )

    assert resp.status_code == 200
    name = db_conn.execute(
        "SELECT match_status, reason_code FROM broadcast_artists WHERE id = %s", (name_id,)
    ).fetchone()
    assert name is not None and (name["match_status"], name["reason_code"]) == (
        "manual_matched",
        None,
    )
    links = {
        row["artist_id"]: (row["state"], row["origin"], row["strength"])
        for row in db_conn.execute(
            "SELECT artist_id, state, origin, strength FROM log_name_links"
            " WHERE broadcast_artist_id = %s",
            (name_id,),
        ).fetchall()
    }
    assert links == {archies: ("active", "manual", 100), other: ("suggested", "automatic", 61)}


def test_a_suggested_name_is_shown_by_default(
    client: TestClient, db_conn: psycopg.Connection[Any]
) -> None:
    suggested = _name(db_conn, "ARCHIES")
    _link(db_conn, suggested, _artist(db_conn, "Archies", "mbid-archies"), 72, origin="legacy")
    bare = _name(db_conn, "ZZZYX QWERTY")  # no suggestion and no song: still not likely
    db_conn.commit()

    resp = client.get(QUEUE, params={"include_unlikely": "false"})

    assert resp.status_code == 200
    body = resp.json()
    assert [item["id"] for item in body["items"]] == [str(suggested)]
    assert body["unlikely_total"] == 1
    assert str(bare) not in {item["id"] for item in body["items"]}
