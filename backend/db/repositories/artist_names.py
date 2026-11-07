from __future__ import annotations

from typing import Any

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.catalog import ArtistName, CatalogStorageError
from backend.domain.enums import ArtistNameSource
from backend.repositories.artist_names import ArtistNameRepository

_ADD_CURRENT_NAME_SQL = """
INSERT INTO artist_names (artist_id, name, normalized_name, source)
SELECT id, name, COALESCE(normalized_name, ''), %s FROM artists WHERE id = %s
ON CONFLICT (artist_id, name, source) DO NOTHING
"""


def add_current_name(
    conn: psycopg.Connection[Any], artist_id: str, source: ArtistNameSource
) -> None:
    """Record the name the artist's row now holds, for each writer of ``artists.name`` (S10). A
    symbol-only name has no normalized name on the artist, and ``""`` here."""
    conn.execute(_ADD_CURRENT_NAME_SQL, (source.value, artist_id))


@translate_lost_connection(CatalogStorageError)
class PgArtistNameRepository(ArtistNameRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def add(
        self, artist_id: str, name: str, normalized_name: str, source: ArtistNameSource
    ) -> bool:
        cur = self._conn.execute(
            """INSERT INTO artist_names (artist_id, name, normalized_name, source)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (artist_id, name, source) DO NOTHING""",
            (artist_id, name, normalized_name, source.value),
        )
        return cur.rowcount == 1

    def list_for_artist(self, artist_id: str) -> list[ArtistName]:
        rows = self._conn.execute(
            """SELECT name, normalized_name, source FROM artist_names
                WHERE artist_id = %s ORDER BY source, name""",
            (artist_id,),
        ).fetchall()
        return [
            ArtistName(r["name"], r["normalized_name"], ArtistNameSource(r["source"])) for r in rows
        ]
