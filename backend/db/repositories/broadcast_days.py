from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

import psycopg

from backend.domain.broadcast import BroadcastDay
from backend.repositories.broadcast_days import BroadcastDayRepository


class PgBroadcastDayRepository(BroadcastDayRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> BroadcastDay:
        return BroadcastDay(
            id=row["id"],
            station_id=row["station_id"],
            broadcast_date=row["broadcast_date"],
        )

    def get_or_create(self, station_id: UUID, broadcast_date: date) -> BroadcastDay:
        row = self._conn.execute(
            "SELECT * FROM broadcast_days WHERE station_id = %s AND broadcast_date = %s",
            (station_id, broadcast_date),
        ).fetchone()
        if row is not None:
            return self._row_to_model(row)
        new_id = uuid4()
        self._conn.execute(
            "INSERT INTO broadcast_days (id, station_id, broadcast_date) VALUES (%s, %s, %s)",
            (new_id, station_id, broadcast_date),
        )
        row = self._conn.execute(
            "SELECT * FROM broadcast_days WHERE id = %s", (new_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_dates_for_station(self, station_id: UUID) -> list[date]:
        rows = self._conn.execute(
            "SELECT broadcast_date FROM broadcast_days"
            " WHERE station_id = %s ORDER BY broadcast_date",
            (station_id,),
        ).fetchall()
        return [r["broadcast_date"] for r in rows]

    def get_by_id(self, day_id: UUID) -> BroadcastDay | None:
        row = self._conn.execute(
            "SELECT * FROM broadcast_days WHERE id = %s", (day_id,)
        ).fetchone()
        return self._row_to_model(row) if row else None
