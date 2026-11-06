"""PostgreSQL adapter for the artist identity sweep's one read of ``library_files`` (phase 2a S14,
D6). Grouped in the database, so a sweep of the whole library reads only the names it can act on."""

from __future__ import annotations

from typing import Any

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.library import ArtistTagGroup, LibraryStorageError
from backend.repositories.artist_tag_files import ArtistTagFilesRepository

_TAG_GROUPS_SQL = """
SELECT normalized_artist_name AS normalized_name,
       artist_mbid AS tag_value,
       artist_name,
       track_title,
       count(*) AS files
  FROM library_files
 WHERE file_status = 'present'
   AND artist_mbid IS NOT NULL
   AND normalized_artist_name IN (
       SELECT normalized_artist_name
         FROM library_files
        WHERE file_status = 'present'
          AND artist_mbid IS NOT NULL
          AND btrim(normalized_artist_name) <> ''
        GROUP BY normalized_artist_name
       HAVING count(DISTINCT lower(artist_mbid)) > 1)
 GROUP BY normalized_artist_name, artist_mbid, artist_name, track_title
"""


@translate_lost_connection(LibraryStorageError)
class PgArtistTagFilesRepository(ArtistTagFilesRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def list_tag_groups(self) -> list[ArtistTagGroup]:
        return [
            ArtistTagGroup(
                normalized_name=row["normalized_name"],
                tag_value=row["tag_value"],
                artist_name=row["artist_name"],
                track_title=row["track_title"],
                files=row["files"],
            )
            for row in self._conn.execute(_TAG_GROUPS_SQL).fetchall()
        ]
