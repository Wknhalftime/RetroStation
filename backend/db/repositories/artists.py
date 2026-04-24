from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import psycopg
import structlog

from backend.domain.catalog import Artist
from backend.domain.enums import CatalogSource
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.artist_enhancement import ArtistEnhancementRepository

logger = structlog.get_logger(__name__)


class PgArtistRepository(ArtistCatalogRepository, ArtistEnhancementRepository):
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
            mbid=row.get("mbid"),
            origin=(
                CatalogSource(row["origin"])
                if row.get("origin")
                else CatalogSource.LOCAL
            ),
            normalized_name=row.get("normalized_name"),
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
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_id(self, artist_id: str) -> Artist | None:
        row = self._conn.execute(
            "SELECT * FROM artists WHERE id = %s", (artist_id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def list_all(self) -> list[Artist]:
        rows = self._conn.execute(
            "SELECT * FROM artists ORDER BY name"
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def list_unenhanced(self) -> list[Artist]:
        rows = self._conn.execute(
            """SELECT * FROM artists
               WHERE needs_enhancement = TRUE
                 AND enhancement_error IS NULL"""
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def mark_enhanced(self, artist_id: str) -> None:
        self._conn.execute(
            "UPDATE artists SET needs_enhancement = FALSE, enhanced_at = now() WHERE id = %s",
            (artist_id,),
        )

    def mark_enhancement_failed(self, artist_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE artists SET enhancement_error = %s WHERE id = %s",
            (error, artist_id),
        )

    def upsert_local_artist(self, name: str, normalized_name: str) -> str:
        row = self._conn.execute(
            """INSERT INTO artists
                   (id, name, sort_name, normalized_name,
                    origin, needs_enhancement)
               VALUES (%s, %s, %s, %s, 'local', FALSE)
               ON CONFLICT (normalized_name) DO NOTHING
               RETURNING id""",
            (str(uuid4()), name, name, normalized_name),
        ).fetchone()
        if row is not None:
            return cast(str, row["id"])
        row = self._conn.execute(
            "SELECT id FROM artists WHERE normalized_name = %s",
            (normalized_name,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"Artist not found after ON CONFLICT: {normalized_name}"
            )
        return cast(str, row["id"])

    def upsert_musicbrainz_artist(
        self,
        mbid: str,
        name: str,
        sort_name: str,
        normalized_name: str,
        disambiguation: str | None = None,
    ) -> str:
        # 1. Return early if this exact MBID is already known.
        row = self._conn.execute(
            "SELECT id FROM artists WHERE mbid = %s", (mbid,)
        ).fetchone()
        if row is not None:
            return cast(str, row["id"])

        # 2. normalized_name collision with a DIFFERENT MBID — do not overwrite.
        existing = self._conn.execute(
            "SELECT id, mbid FROM artists WHERE normalized_name = %s",
            (normalized_name,),
        ).fetchone()
        if (
            existing is not None
            and existing["mbid"] is not None
            and existing["mbid"] != mbid
        ):
            logger.warning(
                "mb_artist_mbid_conflict",
                normalized_name=normalized_name,
                existing_mbid=existing["mbid"],
                incoming_mbid=mbid,
                message=(
                    "Normalized name collision with conflicting MBIDs. "
                    "Keeping existing MBID; skipping overwrite."
                ),
            )
            return cast(str, existing["id"])

        # 3. Safe upsert.
        row = self._conn.execute(
            """INSERT INTO artists
                   (id, name, sort_name, normalized_name, mbid,
                    origin, needs_enhancement, disambiguation)
               VALUES (%s, %s, %s, %s, %s, 'musicbrainz', TRUE, %s)
               ON CONFLICT (normalized_name) DO UPDATE SET
                 mbid = EXCLUDED.mbid,
                 origin = 'musicbrainz',
                 name = EXCLUDED.name,
                 sort_name = EXCLUDED.sort_name,
                 disambiguation = EXCLUDED.disambiguation,
                 needs_enhancement = TRUE
               RETURNING id""",
            (str(uuid4()), name, sort_name, normalized_name, mbid, disambiguation),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Artist upsert_musicbrainz_artist failed: {mbid}")
        return cast(str, row["id"])

    def get_by_normalized_name(
        self, normalized_name: str,
    ) -> Artist | None:
        row = self._conn.execute(
            "SELECT * FROM artists WHERE normalized_name = %s",
            (normalized_name,),
        ).fetchone()
        return self._row_to_model(row) if row else None
