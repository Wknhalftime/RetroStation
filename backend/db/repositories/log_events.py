from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

import psycopg

from backend.domain.models import LogEvent
from backend.repositories.log_events import LogEventRepository


class PgLogEventRepository(LogEventRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> LogEvent:
        return LogEvent(
            id=row["id"],
            identity_id=row["identity_id"],
            playlist_id=row["playlist_id"],
            played_at=row["played_at"],
            broadcast_day_id=row.get("broadcast_day_id"),
        )

    def create(self, event: LogEvent) -> LogEvent:
        self._conn.execute(
            """INSERT INTO log_events (id, identity_id, playlist_id, played_at, broadcast_day_id)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (identity_id, playlist_id, played_at) DO NOTHING""",
            (event.id, event.identity_id, event.playlist_id,
             event.played_at, event.broadcast_day_id),
        )
        row = self._conn.execute(
            """SELECT * FROM log_events
               WHERE identity_id = %s AND playlist_id = %s AND played_at = %s""",
            (event.identity_id, event.playlist_id, event.played_at),
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_playlist(self, playlist_id: UUID) -> list[LogEvent]:
        rows = self._conn.execute(
            "SELECT * FROM log_events WHERE playlist_id = %s ORDER BY played_at",
            (playlist_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_by_identity(self, identity_id: UUID) -> list[LogEvent]:
        rows = self._conn.execute(
            "SELECT * FROM log_events WHERE identity_id = %s ORDER BY played_at",
            (identity_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_by_station_date(self, station_id: UUID, broadcast_date: date) -> list[LogEvent]:
        rows = self._conn.execute(
            """SELECT le.* FROM log_events le
               JOIN playlists p ON p.id = le.playlist_id
               WHERE p.station_id = %s AND le.played_at::date = %s
               ORDER BY le.played_at""",
            (station_id, broadcast_date),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]
