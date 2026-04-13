from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import psycopg

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.broadcast_play_events import PgBroadcastPlayEventRepository
from backend.db.repositories.broadcast_playlists import PgBroadcastPlaylistRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import EnrichmentStatus, MatchStatus
from backend.domain.library import LibraryFile

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _insert_station(conn: psycopg.Connection, call_letters: str = "KAZR-FM") -> BroadcastStation:
    station = BroadcastStation(id=uuid4(), call_letters=call_letters)
    result = PgBroadcastStationRepository(conn).create(station)
    conn.commit()
    return result


def _insert_playlist(
    conn: psycopg.Connection,
    station: BroadcastStation,
    name: str = "show.csv",
) -> BroadcastPlaylist:
    playlist = BroadcastPlaylist(
        id=uuid4(),
        name=name,
        content_hash=uuid4().hex,
        station_id=station.id,
    )
    result = PgBroadcastPlaylistRepository(conn).create(playlist)
    conn.commit()
    return result


def _insert_artist(
    conn: psycopg.Connection,
    original_name: str = "Test Artist",
    match_status: MatchStatus = MatchStatus.NEEDS_REVIEW,
    with_candidates: bool = False,
) -> BroadcastArtist:
    artist = BroadcastArtist(
        id=uuid4(),
        original_name=original_name,
        normalized_name=original_name.lower(),
        match_status=match_status,
        artist_candidates=(
            [{"mbid": "abc-123", "name": original_name, "score": 100}]
            if with_candidates
            else None
        ),
    )
    # upsert won't persist candidates; insert directly so candidates land in JSONB column
    conn.execute(
        """
        INSERT INTO broadcast_artists (id, original_name, normalized_name, match_status, artist_candidates)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            artist.id,
            artist.original_name,
            artist.normalized_name,
            artist.match_status.value,
            json.dumps(artist.artist_candidates) if artist.artist_candidates else None,
        ),
    )
    conn.commit()
    return artist


def _insert_identity(
    conn: psycopg.Connection,
    artist: BroadcastArtist,
    original_title: str = "Test Song",
    match_status: MatchStatus = MatchStatus.PENDING,
) -> BroadcastTrackIdentity:
    identity = BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=artist.id,
        original_title=original_title,
        normalized_title=original_title.lower(),
        normalized_signature=f"{artist.normalized_name}:{original_title.lower()}:{uuid4().hex}",
        match_status=match_status,
    )
    result = PgBroadcastTrackIdentityRepository(conn).upsert(identity)
    conn.commit()
    return result


def _insert_event(
    conn: psycopg.Connection,
    playlist: BroadcastPlaylist,
    identity: BroadcastTrackIdentity,
) -> BroadcastPlayEvent:
    event = BroadcastPlayEvent(
        id=uuid4(),
        identity_id=identity.id,
        playlist_id=playlist.id,
        played_at=datetime.now(tz=UTC),
    )
    result = PgBroadcastPlayEventRepository(conn).create(event)
    conn.commit()
    return result


def _insert_library_file(conn: psycopg.Connection) -> LibraryFile:
    lf = LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4().hex}.flac",
        file_hash=uuid4().hex,
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
    )
    result = PgLibraryFileRepository(conn).upsert(lf)
    conn.commit()
    return result


# ---------------------------------------------------------------------------
# Full seed: station → playlist → artist → identity → event
# ---------------------------------------------------------------------------


def _seed_review_chain(
    db_conn: psycopg.Connection,
    artist_name: str = "Review Artist",
    with_candidates: bool = True,
) -> tuple[BroadcastStation, BroadcastPlaylist, BroadcastArtist, BroadcastTrackIdentity, BroadcastPlayEvent]:
    station = _insert_station(db_conn)
    playlist = _insert_playlist(db_conn, station)
    artist = _insert_artist(db_conn, original_name=artist_name, with_candidates=with_candidates)
    identity = _insert_identity(db_conn, artist)
    event = _insert_event(db_conn, playlist, identity)
    return station, playlist, artist, identity, event


# ---------------------------------------------------------------------------
# TestMatchingQueue
# ---------------------------------------------------------------------------


class TestMatchingQueue:
    def test_returns_artists_needing_review(self, client, db_conn):
        _, _, artist, identity, _ = _seed_review_chain(db_conn, with_candidates=True)

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

        item = data["items"][0]
        assert item["id"] == str(artist.id)
        assert item["original_name"] == "Review Artist"
        assert item["match_status"] == "needs_review"
        assert item["candidates"] is not None
        assert len(item["candidates"]) == 1
        assert item["candidates"][0]["mbid"] == "abc-123"

        assert len(item["identities"]) == 1
        qi = item["identities"][0]
        assert qi["id"] == str(identity.id)
        assert qi["original_title"] == "Test Song"
        assert qi["match_status"] == "pending"
        assert qi["match_tier"] is None

    def test_empty_queue(self, client, db_conn):
        # Insert an artist that is already AUTO_MATCHED (not in queue)
        _insert_artist(db_conn, match_status=MatchStatus.AUTO_MATCHED)

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_pagination(self, client, db_conn):
        for i in range(3):
            _insert_artist(db_conn, original_name=f"Artist {i}")

        resp = client.get("/api/v1/matching/queue?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 2

    def test_pending_artists_included(self, client, db_conn):
        _insert_artist(db_conn, match_status=MatchStatus.PENDING)

        resp = client.get("/api/v1/matching/queue")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1


# ---------------------------------------------------------------------------
# TestResolveArtist
# ---------------------------------------------------------------------------


class TestResolveArtist:
    def test_manual_match(self, client, db_conn):
        _, _, artist, _, _ = _seed_review_chain(db_conn)
        target_mbid = "artist-mbid-0001"

        resp = client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={"match_status": "manual_matched", "target_artist_id": target_mbid},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(artist.id)
        assert data["match_status"] == "manual_matched"

        # Verify DB state: broadcast_artists updated
        row = db_conn.execute(
            "SELECT match_status FROM broadcast_artists WHERE id = %s", (artist.id,)
        ).fetchone()
        assert row is not None
        assert row["match_status"] == "manual_matched"

        # Verify match row created
        match_row = db_conn.execute(
            "SELECT * FROM matches WHERE artist_id = %s", (artist.id,)
        ).fetchone()
        assert match_row is not None
        assert match_row["target_id"] == target_mbid
        assert match_row["target_type"] == "artist"
        assert match_row["confidence_score"] == 1.0
        assert match_row["match_tier"] == "manual"

    def test_manual_reject_cascades(self, client, db_conn):
        _, _, artist, identity, _ = _seed_review_chain(db_conn)

        resp = client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={"match_status": "manual_rejected"},
        )
        assert resp.status_code == 200
        assert resp.json()["match_status"] == "manual_rejected"

        # Artist updated
        artist_row = db_conn.execute(
            "SELECT match_status FROM broadcast_artists WHERE id = %s", (artist.id,)
        ).fetchone()
        assert artist_row is not None
        assert artist_row["match_status"] == "manual_rejected"

        # Child identity cascaded to AUTO_REJECTED
        identity_row = db_conn.execute(
            "SELECT match_status FROM track_identities WHERE id = %s", (identity.id,)
        ).fetchone()
        assert identity_row is not None
        assert identity_row["match_status"] == "auto_rejected"

    def test_manual_reject_does_not_cascade_protected(self, client, db_conn):
        """MANUAL_MATCHED/MANUAL_REJECTED child identities must not be overwritten."""
        _, _, artist, _, _ = _seed_review_chain(db_conn)
        # Add a second identity that is already MANUAL_MATCHED
        protected = _insert_identity(
            db_conn, artist, original_title="Protected Song",
            match_status=MatchStatus.MANUAL_MATCHED,
        )

        client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={"match_status": "manual_rejected"},
        )

        protected_row = db_conn.execute(
            "SELECT match_status FROM track_identities WHERE id = %s", (protected.id,)
        ).fetchone()
        assert protected_row is not None
        assert protected_row["match_status"] == "manual_matched"

    def test_not_found(self, client):
        resp = client.post(
            f"/api/v1/matching/artists/{uuid4()}/resolve",
            json={"match_status": "manual_rejected"},
        )
        assert resp.status_code == 404

    def test_invalid_status(self, client, db_conn):
        _, _, artist, _, _ = _seed_review_chain(db_conn)

        resp = client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={"match_status": "pending"},
        )
        assert resp.status_code == 422

    def test_manual_matched_requires_target(self, client, db_conn):
        _, _, artist, _, _ = _seed_review_chain(db_conn)

        resp = client.post(
            f"/api/v1/matching/artists/{artist.id}/resolve",
            json={"match_status": "manual_matched"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# TestResolveIdentity
# ---------------------------------------------------------------------------


class TestResolveIdentity:
    def test_manual_match(self, client, db_conn):
        _, _, artist, identity, _ = _seed_review_chain(db_conn)
        lib_file = _insert_library_file(db_conn)

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={"match_status": "manual_matched", "library_file_id": str(lib_file.id)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(identity.id)
        assert data["match_status"] == "manual_matched"

        # Verify track_identity updated with MANUAL tier
        id_row = db_conn.execute(
            "SELECT match_status, match_tier FROM track_identities WHERE id = %s",
            (identity.id,),
        ).fetchone()
        assert id_row is not None
        assert id_row["match_status"] == "manual_matched"
        assert id_row["match_tier"] == "manual"

        # Verify match row created
        match_row = db_conn.execute(
            "SELECT * FROM matches WHERE identity_id = %s", (identity.id,)
        ).fetchone()
        assert match_row is not None
        assert match_row["library_file_id"] == lib_file.id
        assert match_row["target_type"] == "library_file"
        assert match_row["confidence_score"] == 1.0
        assert match_row["match_tier"] == "manual"

    def test_manual_reject(self, client, db_conn):
        _, _, artist, identity, _ = _seed_review_chain(db_conn)

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={"match_status": "manual_rejected"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["match_status"] == "manual_rejected"

        id_row = db_conn.execute(
            "SELECT match_status FROM track_identities WHERE id = %s", (identity.id,)
        ).fetchone()
        assert id_row is not None
        assert id_row["match_status"] == "manual_rejected"

    def test_manual_match_replaces_existing_match(self, client, db_conn):
        _, _, artist, identity, _ = _seed_review_chain(db_conn)
        lib_file_old = _insert_library_file(db_conn)
        lib_file_new = _insert_library_file(db_conn)

        # Create an initial match row
        db_conn.execute(
            """
            INSERT INTO matches (id, identity_id, library_file_id, target_type, confidence_score, match_tier)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                uuid4(),
                identity.id,
                lib_file_old.id,
                "library_file",
                0.85,
                "vector",
            ),
        )
        db_conn.commit()

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={"match_status": "manual_matched", "library_file_id": str(lib_file_new.id)},
        )
        assert resp.status_code == 200

        # Only one match row should exist and it should be the new one
        rows = db_conn.execute(
            "SELECT * FROM matches WHERE identity_id = %s", (identity.id,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["library_file_id"] == lib_file_new.id
        assert rows[0]["match_tier"] == "manual"

    def test_not_found(self, client):
        resp = client.post(
            f"/api/v1/matching/identities/{uuid4()}/resolve",
            json={"match_status": "manual_rejected"},
        )
        assert resp.status_code == 404

    def test_invalid_status(self, client, db_conn):
        _, _, artist, identity, _ = _seed_review_chain(db_conn)

        resp = client.post(
            f"/api/v1/matching/identities/{identity.id}/resolve",
            json={"match_status": "auto_matched"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# TestMatchingRun
# ---------------------------------------------------------------------------


class TestMatchingRun:
    def test_accepted(self, client):
        resp = client.post("/api/v1/matching/run")
        assert resp.status_code == 202

    def test_returns_count(self, client, db_conn):
        # Seed two playlists with unresolved artists (distinct stations to avoid unique violation)
        for i in range(2):
            station = _insert_station(db_conn, call_letters=f"KRUN-{i}")
            playlist = _insert_playlist(db_conn, station, name=f"run_{i}.csv")
            artist = _insert_artist(db_conn, original_name=f"Run Artist {i}")
            identity = _insert_identity(db_conn, artist)
            _insert_event(db_conn, playlist, identity)

        resp = client.post("/api/v1/matching/run")
        assert resp.status_code == 202
        data = resp.json()
        assert "count" in data
        assert isinstance(data["count"], int)
        assert data["count"] >= 1
