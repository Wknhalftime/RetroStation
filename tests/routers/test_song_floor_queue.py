"""Acceptance tests: the queue's song triage at the 56% floor (spec 2026-10-05 §4.3, D6).

The router buckets and the SQL CTE (bucket and `likely`) both start "needs_attention" at
SONG_MIN_PRESENTATION_SCORE (56); 65+ is "quick_review". The queue lists only pending /
needs_review work, so a song the matcher floored to auto_rejected leaves it.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import DictRow

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import MatchStatus
from tests.routers.test_matching import (
    _insert_artist,
    _insert_identity,
    _insert_library_file,
    _insert_match_row,
)

type Conn = psycopg.Connection[DictRow]

QUEUE = "/api/v1/matching/queue"

# Score -> the bucket both the Python triage and the SQL CTE must give it.
EXPECTED = {
    30.0: "blocked",
    55.99: "blocked",
    56.0: "needs_attention",
    64.99: "needs_attention",
    65.0: "quick_review",
}


def _review_song(
    db_conn: Conn, name: str, score: float, *, with_file: bool = False
) -> tuple[BroadcastArtist, BroadcastTrackIdentity]:
    """A matched artist with one needs_review song whose best guess scored ``score``."""
    artist = _insert_artist(db_conn, original_name=name, match_status=MatchStatus.AUTO_MATCHED)
    song = _insert_identity(db_conn, artist, "Floor Song", MatchStatus.NEEDS_REVIEW)
    file_id = None
    if with_file:
        file_id = _insert_library_file(
            db_conn, track_title="Floor Song", artist_name=name, normalized_artist_name=name.lower()
        ).id
    _insert_match_row(db_conn, song, score, library_file_id=file_id)
    return artist, song


def test_python_and_sql_buckets_agree_at_the_floor(client: TestClient, db_conn: Conn) -> None:
    expected = {
        str(_review_song(db_conn, f"Edge {score}", score)[0].id): bucket
        for score, bucket in EXPECTED.items()
    }

    listed = client.get(QUEUE, params={"limit": 500}).json()["items"]
    assert {a["id"]: a["triage_bucket"] for a in listed} == expected
    for bucket in ("blocked", "needs_attention", "quick_review"):
        page = client.get(QUEUE, params={"bucket": bucket, "limit": 500}).json()
        assert {a["id"] for a in page["items"]} == {
            aid for aid, b in expected.items() if b == bucket
        }, bucket


def test_a_song_just_under_the_floor_does_not_make_its_artist_likely(
    client: TestClient, db_conn: Conn
) -> None:
    _review_song(db_conn, "Under", 55.99)

    data = client.get(QUEUE, params={"include_unlikely": "false"}).json()

    assert data["items"] == []
    assert data["total"] == 0
    assert data["unlikely_total"] == 1


def test_a_song_at_the_floor_makes_its_artist_likely(client: TestClient, db_conn: Conn) -> None:
    artist, _ = _review_song(db_conn, "At", 56.0)

    data = client.get(QUEUE, params={"include_unlikely": "false"}).json()

    assert [a["id"] for a in data["items"]] == [str(artist.id)]
    assert data["unlikely_total"] == 0


@pytest.mark.parametrize(("score", "shown"), [(55.99, False), (56.0, True)])
def test_a_proposal_is_shown_from_the_floor_up(
    client: TestClient, db_conn: Conn, score: float, shown: bool
) -> None:
    _review_song(db_conn, "Proposal", score, with_file=True)

    song = client.get(QUEUE).json()["items"][0]["identities"][0]

    assert (song["proposed_match"] is not None) is shown


def test_an_artist_whose_only_open_song_was_floored_leaves_the_queue(
    client: TestClient, db_conn: Conn
) -> None:
    artist = _insert_artist(db_conn, original_name="Floored", match_status=MatchStatus.AUTO_MATCHED)
    _insert_identity(db_conn, artist, "Gone", MatchStatus.AUTO_REJECTED)

    data = client.get(QUEUE).json()

    assert data["items"] == []
    assert data["total"] == 0
    assert data["unlikely_total"] == 0


def test_a_floored_song_is_listed_as_resolved_beside_an_open_one(
    client: TestClient, db_conn: Conn
) -> None:
    artist, _ = _review_song(db_conn, "Mixed", 70.0)
    floored = _insert_identity(db_conn, artist, "Floored Song", MatchStatus.AUTO_REJECTED)

    item = client.get(QUEUE).json()["items"][0]

    assert item["triage_bucket"] == "quick_review"
    shown = {i["id"]: i for i in item["identities"]}[str(floored.id)]
    assert shown["match_status"] == "auto_rejected"
    assert shown["confidence_score"] is None
    assert shown["proposed_match"] is None
