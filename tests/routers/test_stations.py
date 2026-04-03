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


def _insert_station(
    conn: psycopg.Connection, call_letters: str, **kwargs
) -> Station:
    station = Station(id=uuid4(), call_letters=call_letters, **kwargs)
    result = PgStationRepository(conn).create(station)
    conn.commit()
    return result


def _insert_playlist(conn, station, name="show.csv"):
    playlist = Playlist(
        id=uuid4(), name=name, content_hash=uuid4().hex, station_id=station.id,
    )
    result = PgPlaylistRepository(conn).create(playlist)
    conn.commit()
    return result


def _insert_event_full(conn, playlist, artist_name="Test Artist", title="Test Song", played_at=None):
    """Insert artist + identity + event. Returns the event."""
    artist = LogArtist(
        id=uuid4(), original_name=artist_name,
        normalized_name=artist_name.lower(), match_status=MatchStatus.PENDING,
    )
    PgLogArtistRepository(conn).upsert(artist)

    identity = LogIdentity(
        id=uuid4(), artist_id=artist.id, original_title=title,
        normalized_title=title.lower(),
        normalized_signature=f"{artist_name.lower()}:{title.lower()}",
        match_status=MatchStatus.PENDING,
    )
    PgLogIdentityRepository(conn).upsert(identity)

    event = LogEvent(
        id=uuid4(), identity_id=identity.id, playlist_id=playlist.id,
        played_at=played_at or datetime.now(tz=timezone.utc),
    )
    PgLogEventRepository(conn).create(event)
    conn.commit()
    return event


class TestListStations:
    def test_empty(self, client):
        resp = client.get("/api/v1/stations")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_all(self, client, db_conn):
        _insert_station(db_conn, "KAZR-FM", name="Laser 103.3", city="Waukee")
        _insert_station(db_conn, "KIOA-FM", name="KIOA", city="Des Moines")
        resp = client.get("/api/v1/stations")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["call_letters"] == "KAZR-FM"
        assert data[1]["call_letters"] == "KIOA-FM"

    def test_includes_playlist_count(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        from backend.db.repositories.playlists import PgPlaylistRepository
        from backend.domain.models import Playlist
        PgPlaylistRepository(db_conn).create(
            Playlist(id=uuid4(), name="test.csv", content_hash="abc123", station_id=station.id)
        )
        db_conn.commit()
        resp = client.get("/api/v1/stations")
        data = resp.json()
        assert data[0]["playlist_count"] == 1


class TestCreateStation:
    def test_create(self, client):
        resp = client.post("/api/v1/stations", json={
            "call_letters": "KAZR-FM", "name": "Laser 103.3",
            "city": "Waukee", "format_name": "CHR",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["call_letters"] == "KAZR-FM"
        assert "id" in data

    def test_duplicate_call_letters_409(self, client, db_conn):
        _insert_station(db_conn, "KAZR-FM")
        resp = client.post("/api/v1/stations", json={"call_letters": "KAZR-FM"})
        assert resp.status_code == 409


class TestGetStation:
    def test_found(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM", name="Laser 103.3")
        resp = client.get(f"/api/v1/stations/{station.id}")
        assert resp.status_code == 200
        assert resp.json()["call_letters"] == "KAZR-FM"

    def test_not_found(self, client):
        resp = client.get(f"/api/v1/stations/{uuid4()}")
        assert resp.status_code == 404


class TestUpdateStation:
    def test_update(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM", name="Old Name")
        resp = client.put(f"/api/v1/stations/{station.id}", json={"name": "Laser 103.3"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Laser 103.3"
        assert resp.json()["call_letters"] == "KAZR-FM"

    def test_not_found(self, client):
        resp = client.put(f"/api/v1/stations/{uuid4()}", json={"name": "x"})
        assert resp.status_code == 404


class TestDeleteStation:
    def test_delete(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        resp = client.delete(f"/api/v1/stations/{station.id}")
        assert resp.status_code == 204
        resp = client.get(f"/api/v1/stations/{station.id}")
        assert resp.status_code == 404

    def test_not_found(self, client):
        resp = client.delete(f"/api/v1/stations/{uuid4()}")
        assert resp.status_code == 404


class TestStationBroadcastDays:
    def test_returns_dates(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        repo = PgBroadcastDayRepository(db_conn)
        repo.get_or_create(station.id, date(2001, 3, 15))
        repo.get_or_create(station.id, date(2001, 6, 20))
        db_conn.commit()

        resp = client.get(f"/api/v1/stations/{station.id}/broadcast-days")
        assert resp.status_code == 200
        assert resp.json() == ["2001-03-15", "2001-06-20"]

    def test_empty(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        resp = client.get(f"/api/v1/stations/{station.id}/broadcast-days")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_not_found(self, client):
        resp = client.get(f"/api/v1/stations/{uuid4()}/broadcast-days")
        assert resp.status_code == 404


class TestStationEventsByDate:
    def test_returns_events(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station, name="morning.csv")
        _insert_event_full(
            db_conn, playlist, "The Clash", "London Calling",
            played_at=datetime(2001, 3, 15, 8, 0, 0, tzinfo=timezone.utc),
        )
        _insert_event_full(
            db_conn, playlist, "Nirvana", "Smells Like Teen Spirit",
            played_at=datetime(2001, 3, 15, 9, 0, 0, tzinfo=timezone.utc),
        )

        resp = client.get(f"/api/v1/stations/{station.id}/events?date=2001-03-15")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["items"][0]["artist_name"] == "The Clash"
        assert data["items"][0]["playlist_name"] == "morning.csv"

    def test_cross_playlist(self, client, db_conn):
        """Events from multiple playlists on the same date are returned."""
        station = _insert_station(db_conn, "KAZR-FM")
        p1 = _insert_playlist(db_conn, station, name="morning.csv")
        p2 = _insert_playlist(db_conn, station, name="evening.csv")
        _insert_event_full(
            db_conn, p1, "The Clash", "London Calling",
            played_at=datetime(2001, 3, 15, 8, 0, 0, tzinfo=timezone.utc),
        )
        _insert_event_full(
            db_conn, p2, "Nirvana", "In Bloom",
            played_at=datetime(2001, 3, 15, 20, 0, 0, tzinfo=timezone.utc),
        )

        resp = client.get(f"/api/v1/stations/{station.id}/events?date=2001-03-15")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        names = {item["playlist_name"] for item in data["items"]}
        assert names == {"morning.csv", "evening.csv"}

    def test_excludes_other_dates(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        _insert_event_full(
            db_conn, playlist, "The Clash", "London Calling",
            played_at=datetime(2001, 3, 15, 8, 0, 0, tzinfo=timezone.utc),
        )
        _insert_event_full(
            db_conn, playlist, "Nirvana", "In Bloom",
            played_at=datetime(2001, 3, 16, 8, 0, 0, tzinfo=timezone.utc),
        )

        resp = client.get(f"/api/v1/stations/{station.id}/events?date=2001-03-15")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_pagination(self, client, db_conn):
        station = _insert_station(db_conn, "KAZR-FM")
        playlist = _insert_playlist(db_conn, station)
        for i in range(5):
            _insert_event_full(
                db_conn, playlist, f"Artist {i}", f"Song {i}",
                played_at=datetime(2001, 3, 15, 8, i, 0, tzinfo=timezone.utc),
            )

        resp = client.get(f"/api/v1/stations/{station.id}/events?date=2001-03-15&limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_not_found(self, client):
        resp = client.get(f"/api/v1/stations/{uuid4()}/events?date=2001-03-15")
        assert resp.status_code == 404


class TestStationExportM3u:
    def test_export_empty_date(self, client, db_conn):
        """A station with no events on that date returns a valid M3U header."""
        station = _insert_station(db_conn, "KAZR-FM")
        resp = client.post(
            f"/api/v1/stations/{station.id}/export-m3u",
            json={"date": "2001-03-15"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("audio/x-mpegurl")
        assert "Content-Disposition" in resp.headers
        assert "KAZR-FM-2001-03-15.m3u" in resp.headers["Content-Disposition"]
        assert resp.text.startswith("#EXTM3U")

    def test_not_found(self, client):
        resp = client.post(
            f"/api/v1/stations/{uuid4()}/export-m3u",
            json={"date": "2001-03-15"},
        )
        assert resp.status_code == 404
