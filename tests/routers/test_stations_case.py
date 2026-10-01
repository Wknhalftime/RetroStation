"""Stations cannot differ only by case, over the stations API (spec D72: "a station differing
from another only by case is blocked (... a duplicate rename answers 409, not 500)"; stored
casing is kept as typed; C1: routes map domain errors only)."""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.domain.broadcast import BroadcastStation

pytestmark = pytest.mark.integration

STATIONS = "/api/v1/stations"


def stored(conn: psycopg.Connection[dict[str, object]], call_letters: str) -> UUID:
    station = PgBroadcastStationRepository(conn).create(
        BroadcastStation(id=uuid4(), call_letters=call_letters)
    )
    conn.commit()
    return station.id


def call_letters_of(client: TestClient, station_id: UUID) -> str:
    response = client.get(f"{STATIONS}/{station_id}")
    assert response.status_code == 200
    return str(response.json()["call_letters"])


def test_adding_a_station_differing_only_in_case_is_409(
    client: TestClient, db_conn: psycopg.Connection[dict[str, object]]
) -> None:
    stored(db_conn, "KAZR-FM")
    response = client.post(STATIONS, json={"call_letters": "kazr-fm"})
    assert response.status_code == 409
    assert response.json() == {"detail": "Station with call_letters 'kazr-fm' already exists"}


@pytest.mark.parametrize("taken", ["KAZR-FM", "kazr-fm"], ids=["exact", "case"])
def test_renaming_onto_another_stations_call_letters_is_409(
    client: TestClient, db_conn: psycopg.Connection[dict[str, object]], taken: str
) -> None:
    stored(db_conn, "KAZR-FM")
    renamed = stored(db_conn, "KIOA-FM")
    response = client.put(f"{STATIONS}/{renamed}", json={"call_letters": taken})
    assert response.status_code == 409  # not 500
    assert response.json() == {"detail": f"Station with call_letters '{taken}' already exists"}


def test_a_refused_rename_changes_nothing(
    client: TestClient, db_conn: psycopg.Connection[dict[str, object]]
) -> None:
    kazr = stored(db_conn, "KAZR-FM")
    kioa = stored(db_conn, "KIOA-FM")
    response = client.put(f"{STATIONS}/{kioa}", json={"call_letters": "kazr-fm", "name": "X"})
    assert response.status_code == 409
    assert call_letters_of(client, kazr) == "KAZR-FM"
    assert call_letters_of(client, kioa) == "KIOA-FM"
    assert client.get(f"{STATIONS}/{kioa}").json()["name"] is None


def test_a_station_may_change_the_case_of_its_own_call_letters(
    client: TestClient, db_conn: psycopg.Connection[dict[str, object]]
) -> None:
    kioa = stored(db_conn, "KIOA-FM")
    response = client.put(f"{STATIONS}/{kioa}", json={"call_letters": "Kioa-FM"})
    assert response.status_code == 200
    assert call_letters_of(client, kioa) == "Kioa-FM"
