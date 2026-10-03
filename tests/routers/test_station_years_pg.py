"""The station-year list read from PostgreSQL through the app's own wiring (spec D70: days
logged only, from the logged broadcast days (plan R2: a ``broadcast_days`` row is a
logged day); Testing: "repositories against PostgreSQL";
the composition root wires the list's repositories)."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.db.repositories.broadcast_days import PgBroadcastDayRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.domain.broadcast import BroadcastStation

pytestmark = pytest.mark.integration


def logged(conn: psycopg.Connection[dict[str, object]], call_letters: str, *days: date) -> None:
    station = PgBroadcastStationRepository(conn).create(
        BroadcastStation(id=uuid4(), call_letters=call_letters)
    )
    for day in days:
        PgBroadcastDayRepository(conn).get_or_create(station.id, day)
    conn.commit()


def test_the_list_reads_stations_and_logged_days_from_postgres(
    client: TestClient, db_conn: psycopg.Connection[dict[str, object]]
) -> None:
    logged(db_conn, "KIOA", date(1995, 3, 14), date(1995, 3, 15), date(1996, 1, 1))
    logged(db_conn, "KSTZ", date(2001, 9, 1))
    logged(db_conn, "KNRK")
    response = client.get("/radio/station-years")
    assert response.status_code == 200
    assert response.json() == [
        {"call_letters": "KIOA", "year": 1995, "days_logged": 2, "days_in_year": 365},
        {"call_letters": "KIOA", "year": 1996, "days_logged": 1, "days_in_year": 366},
        {"call_letters": "KSTZ", "year": 2001, "days_logged": 1, "days_in_year": 365},
    ]
