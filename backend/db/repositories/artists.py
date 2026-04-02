from __future__ import annotations

from typing import Any

import psycopg

from backend.domain.models import Artist
from backend.repositories.artists import ArtistRepository


class PgArtistRepository(ArtistRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> Artist:
        return Artist(
            id=row["id"],
            name=row["name"],
            sort_name=row["sort_name"],
            disambiguation=row.get("disambiguation"),
            needs_enhancement=row["needs_enhancement"],
            enhanced_at=row.get("enhanced_at"),
            enhancement_error=row.get("enhancement_error"),
        )

    def upsert(self, artist: Artist) -> Artist:
        self._conn.execute(
            """INSERT INTO artists (id, name, sort_name, disambiguation)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                 name = EXCLUDED.name,
                 sort_name = EXCLUDED.sort_name,
                 disambiguation = EXCLUDED.disambiguation""",
            (artist.id, artist.name, artist.sort_name, artist.disambiguation),
        )
        row = self._conn.execute(
            "SELECT * FROM artists WHERE id = %s", (artist.id,)
        ).fetchone()
        assert row is not None
        return self._row_to_model(row)

    def get_by_id(self, mbid: str) -> Artist | None:
        row = self._conn.execute(
            "SELECT * FROM artists WHERE id = %s", (mbid,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def list_all(self) -> list[Artist]:
        rows = self._conn.execute(
            "SELECT * FROM artists ORDER BY name"
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def list_needing_enhancement(self) -> list[Artist]:
        rows = self._conn.execute(
            "SELECT * FROM artists WHERE needs_enhancement = TRUE"
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def mark_enhanced(self, mbid: str) -> None:
        self._conn.execute(
            "UPDATE artists SET needs_enhancement = FALSE, enhanced_at = now() WHERE id = %s",
            (mbid,),
        )

    def mark_enhancement_failed(self, mbid: str, error: str) -> None:
        self._conn.execute(
            "UPDATE artists SET enhancement_error = %s WHERE id = %s",
            (error, mbid),
        )
