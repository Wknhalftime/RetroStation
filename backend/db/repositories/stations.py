from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.domain.models import Station
from backend.repositories.stations import StationRepository


class PgStationRepository(StationRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> Station:
        return Station(
            id=row["id"],
            call_letters=row["call_letters"],
            name=row.get("name"),
            city=row.get("city"),
            format_name=row.get("format_name"),
            created_at=row["created_at"],
        )

    def create(self, station: Station) -> Station:
        self._conn.execute(
            """INSERT INTO stations (id, call_letters, name, city, format_name)
               VALUES (%s, %s, %s, %s, %s)""",
            (station.id, station.call_letters, station.name, station.city,
             station.format_name),
        )
        row = self._conn.execute(
            "SELECT * FROM stations WHERE id = %s", (station.id,)
        ).fetchone()
        assert row is not None
        return self._row_to_model(row)

    def get_by_id(self, id: UUID) -> Station | None:
        row = self._conn.execute(
            "SELECT * FROM stations WHERE id = %s", (id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_call_letters(self, call_letters: str) -> Station | None:
        row = self._conn.execute(
            "SELECT * FROM stations WHERE call_letters = %s", (call_letters,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def list_all(self) -> list[Station]:
        rows = self._conn.execute(
            "SELECT * FROM stations ORDER BY call_letters"
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def update(self, station: Station) -> Station:
        self._conn.execute(
            """UPDATE stations
               SET call_letters = %s, name = %s, city = %s, format_name = %s
               WHERE id = %s""",
            (station.call_letters, station.name, station.city,
             station.format_name, station.id),
        )
        row = self._conn.execute(
            "SELECT * FROM stations WHERE id = %s", (station.id,)
        ).fetchone()
        assert row is not None
        return self._row_to_model(row)

    def delete(self, id: UUID) -> None:
        self._conn.execute("DELETE FROM stations WHERE id = %s", (id,))
