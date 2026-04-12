from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.domain.broadcast import BroadcastPlaylist
from backend.repositories.playlists import BroadcastPlaylistRepository


class PgBroadcastPlaylistRepository(BroadcastPlaylistRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> BroadcastPlaylist:
        return BroadcastPlaylist(
            id=row["id"],
            name=row["name"],
            content_hash=row["content_hash"],
            ingested_at=row["ingested_at"],
            station_id=row.get("station_id"),
        )

    def create(self, playlist: BroadcastPlaylist) -> BroadcastPlaylist:
        self._conn.execute(
            """INSERT INTO playlists (id, name, content_hash, station_id)
               VALUES (%s, %s, %s, %s)""",
            (playlist.id, playlist.name, playlist.content_hash, playlist.station_id),
        )
        row = self._conn.execute(
            "SELECT * FROM playlists WHERE id = %s", (playlist.id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_id(self, id: UUID) -> BroadcastPlaylist | None:
        row = self._conn.execute(
            "SELECT * FROM playlists WHERE id = %s", (id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_content_hash(self, content_hash: str) -> BroadcastPlaylist | None:
        row = self._conn.execute(
            "SELECT * FROM playlists WHERE content_hash = %s", (content_hash,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def list_by_station(self, station_id: UUID) -> list[BroadcastPlaylist]:
        rows = self._conn.execute(
            "SELECT * FROM playlists WHERE station_id = %s ORDER BY ingested_at",
            (station_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]
