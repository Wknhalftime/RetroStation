from __future__ import annotations

from uuid import uuid4

import psycopg

from backend.db.repositories.stations import PgStationRepository
from backend.domain.models import Station


def _insert_station(
    conn: psycopg.Connection, call_letters: str, **kwargs
) -> Station:
    station = Station(id=uuid4(), call_letters=call_letters, **kwargs)
    result = PgStationRepository(conn).create(station)
    conn.commit()
    return result


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
