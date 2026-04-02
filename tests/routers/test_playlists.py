from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

import psycopg

from backend.db.repositories.broadcast_days import PgBroadcastDayRepository
from backend.db.repositories.log_artists import PgLogArtistRepository
from backend.db.repositories.log_events import PgLogEventRepository
from backend.db.repositories.log_identities import PgLogIdentityRepository
from backend.db.repositories.playlists import PgPlaylistRepository
from backend.db.repositories.stations import PgStationRepository
from backend.domain.enums import MatchStatus
from backend.domain.models import (
    LogArtist,
    LogEvent,
    LogIdentity,
    Playlist,
    Station,
)


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _insert_station(conn: psycopg.Connection, call_letters: str = "KAZR-FM") -> Station:
    station = Station(id=uuid4(), call_letters=call_letters)
    result = PgStationRepository(conn).create(station)
    conn.commit()
    return result


def _insert_playlist(
    conn: psycopg.Connection,
    station: Station | None = None,
    name: str = "show.csv",
    content_hash: str | None = None,
) -> Playlist:
    playlist = Playlist(
        id=uuid4(),
        name=name,
        content_hash=content_hash or uuid4().hex,
        station_id=station.id if station else None,
    )
    result = PgPlaylistRepository(conn).create(playlist)
    conn.commit()
    return result


def _insert_artist(
    conn: psycopg.Connection,
    original_name: str = "Test Artist",
    normalized_name: str | None = None,
) -> LogArtist:
    artist = LogArtist(
        id=uuid4(),
        original_name=original_name,
        normalized_name=normalized_name or original_name.lower(),
        match_status=MatchStatus.PENDING,
    )
    result = PgLogArtistRepository(conn).upsert(artist)
    conn.commit()
    return result


def _insert_identity(
    conn: psycopg.Connection,
    artist: LogArtist,
    original_title: str = "Test Song",
    normalized_signature: str | None = None,
) -> LogIdentity:
    identity = LogIdentity(
        id=uuid4(),
        artist_id=artist.id,
        original_title=original_title,
        normalized_title=original_title.lower(),
        normalized_signature=normalized_signature or f"{artist.normalized_name}:{original_title.lower()}",
        match_status=MatchStatus.PENDING,
    )
    result = PgLogIdentityRepository(conn).upsert(identity)
    conn.commit()
    return result


def _insert_event(
    conn: psycopg.Connection,
    playlist: Playlist,
    identity: LogIdentity,
    played_at: datetime | None = None,
) -> LogEvent:
    event = LogEvent(
        id=uuid4(),
        identity_id=identity.id,
        playlist_id=playlist.id,
        played_at=played_at or datetime.now(tz=timezone.utc),
    )
    result = PgLogEventRepository(conn).create(event)
    conn.commit()
    return result


# ---------------------------------------------------------------------------
# TestListPlaylists
# ---------------------------------------------------------------------------


class TestListPlaylists:
    def test_by_station(self, client, db_conn):
        station = _insert_station(db_conn)
        _insert_playlist(db_conn, station=station, name="morning.csv")
        _insert_playlist(db_conn, station=station, name="evening.csv")

        resp = client.get(f"/api/v1/playlists?station_id={station.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        names = {p["name"] for p in data}
        assert names == {"morning.csv", "evening.csv"}

    def test_by_station_includes_event_count(self, client, db_conn):
        station = _insert_station(db_conn)
        playlist = _insert_playlist(db_conn, station=station)
        artist = _insert_artist(db_conn)
        identity = _insert_identity(db_conn, artist)
        _insert_event(db_conn, playlist, identity)

        resp = client.get(f"/api/v1/playlists?station_id={station.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["event_count"] == 1

    def test_requires_station_id(self, client):
        resp = client.get("/api/v1/playlists")
        assert resp.status_code == 422

    def test_only_returns_playlists_for_given_station(self, client, db_conn):
        station_a = _insert_station(db_conn, "KAZR-FM")
        station_b = _insert_station(db_conn, "KIOA-FM")
        _insert_playlist(db_conn, station=station_a, name="a.csv")
        _insert_playlist(db_conn, station=station_b, name="b.csv")

        resp = client.get(f"/api/v1/playlists?station_id={station_a.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "a.csv"


# ---------------------------------------------------------------------------
# TestGetPlaylist
# ---------------------------------------------------------------------------


class TestGetPlaylist:
    def test_found(self, client, db_conn):
        station = _insert_station(db_conn)
        playlist = _insert_playlist(db_conn, station=station, name="show.csv")

        resp = client.get(f"/api/v1/playlists/{playlist.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(playlist.id)
        assert data["name"] == "show.csv"
        assert data["station_id"] == str(station.id)
        assert "content_hash" in data
        assert "ingested_at" in data

    def test_not_found(self, client):
        resp = client.get(f"/api/v1/playlists/{uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestPlaylistEvents
# ---------------------------------------------------------------------------


class TestPlaylistEvents:
    def _seed_playlist_with_events(
        self, db_conn, count: int = 5
    ) -> tuple:
        station = _insert_station(db_conn)
        playlist = _insert_playlist(db_conn, station=station)
        artist = _insert_artist(db_conn)
        for i in range(count):
            identity = _insert_identity(
                db_conn,
                artist,
                original_title=f"Song {i}",
                normalized_signature=f"test artist:song {i}",
            )
            _insert_event(
                db_conn,
                playlist,
                identity,
                played_at=datetime(2024, 1, 1, 12, i, 0, tzinfo=timezone.utc),
            )
        return station, playlist, artist

    def test_paginated(self, client, db_conn):
        _, playlist, _ = self._seed_playlist_with_events(db_conn, count=5)

        resp = client.get(f"/api/v1/playlists/{playlist.id}/events?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_default_pagination(self, client, db_conn):
        _, playlist, _ = self._seed_playlist_with_events(db_conn, count=3)

        resp = client.get(f"/api/v1/playlists/{playlist.id}/events")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_event_item_fields(self, client, db_conn):
        station = _insert_station(db_conn)
        playlist = _insert_playlist(db_conn, station=station)
        artist = _insert_artist(db_conn, original_name="The Clash")
        identity = _insert_identity(db_conn, artist, original_title="London Calling")
        _insert_event(db_conn, playlist, identity)

        resp = client.get(f"/api/v1/playlists/{playlist.id}/events")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["artist_name"] == "The Clash"
        assert item["title"] == "London Calling"
        assert item["match_status"] == "PENDING"
        assert item["match_tier"] is None
        assert "id" in item
        assert "played_at" in item

    def test_offset_pagination(self, client, db_conn):
        _, playlist, _ = self._seed_playlist_with_events(db_conn, count=5)

        resp = client.get(f"/api/v1/playlists/{playlist.id}/events?limit=2&offset=3")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_not_found(self, client):
        resp = client.get(f"/api/v1/playlists/{uuid4()}/events")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestBroadcastDays
# ---------------------------------------------------------------------------


class TestBroadcastDays:
    def test_returns_dates(self, client, db_conn):
        station = _insert_station(db_conn)
        playlist = _insert_playlist(db_conn, station=station)
        repo = PgBroadcastDayRepository(db_conn)
        repo.get_or_create(station.id, date(2024, 3, 15))
        repo.get_or_create(station.id, date(2024, 3, 16))
        db_conn.commit()

        resp = client.get(f"/api/v1/playlists/{playlist.id}/broadcast-days")
        assert resp.status_code == 200
        dates = resp.json()
        assert dates == ["2024-03-15", "2024-03-16"]

    def test_returns_empty_when_no_days(self, client, db_conn):
        station = _insert_station(db_conn)
        playlist = _insert_playlist(db_conn, station=station)

        resp = client.get(f"/api/v1/playlists/{playlist.id}/broadcast-days")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_empty_when_no_station(self, client, db_conn):
        # Playlist with no station_id
        playlist = _insert_playlist(db_conn, station=None)

        resp = client.get(f"/api/v1/playlists/{playlist.id}/broadcast-days")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_not_found(self, client):
        resp = client.get(f"/api/v1/playlists/{uuid4()}/broadcast-days")
        assert resp.status_code == 404
