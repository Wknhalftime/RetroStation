from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

import psycopg

from backend.domain.broadcast import BroadcastPlayEvent
from backend.repositories.broadcast_play_events import BroadcastPlayEventRepository


class PgBroadcastPlayEventRepository(BroadcastPlayEventRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> BroadcastPlayEvent:
        return BroadcastPlayEvent(
            id=row["id"],
            identity_id=row["identity_id"],
            playlist_id=row["playlist_id"],
            played_at=row["played_at"],
            broadcast_day_id=row.get("broadcast_day_id"),
        )

    def create(self, event: BroadcastPlayEvent) -> BroadcastPlayEvent:
        self._conn.execute(
            """INSERT INTO play_events
               (id, identity_id, playlist_id, played_at, broadcast_day_id)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (identity_id, playlist_id, played_at) DO NOTHING""",
            (event.id, event.identity_id, event.playlist_id,
             event.played_at, event.broadcast_day_id),
        )
        row = self._conn.execute(
            """SELECT * FROM play_events
               WHERE identity_id = %s AND playlist_id = %s
                 AND played_at = %s""",
            (event.identity_id, event.playlist_id, event.played_at),
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_playlist(self, playlist_id: UUID) -> list[BroadcastPlayEvent]:
        rows = self._conn.execute(
            "SELECT * FROM play_events WHERE playlist_id = %s ORDER BY played_at",
            (playlist_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_by_identity(self, identity_id: UUID) -> list[BroadcastPlayEvent]:
        rows = self._conn.execute(
            "SELECT * FROM play_events WHERE identity_id = %s ORDER BY played_at",
            (identity_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_by_station_date(
        self, station_id: UUID, broadcast_date: date
    ) -> list[BroadcastPlayEvent]:
        rows = self._conn.execute(
            """SELECT le.* FROM play_events le
               JOIN playlists p ON p.id = le.playlist_id
               WHERE p.station_id = %s AND le.played_at::date = %s
               ORDER BY le.played_at""",
            (station_id, broadcast_date),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

