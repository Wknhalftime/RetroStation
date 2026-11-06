from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from uuid import uuid4

import psycopg

from backend.db.repositories.artist_names import add_current_name
from backend.domain.catalog import Artist, name_mbids_of
from backend.domain.enums import ArtistLinkOutcome, ArtistNameSource, CatalogSource
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.artist_enhancement import ArtistEnhancementRepository


def artist_from_row(row: dict[str, Any]) -> Artist:
    """Map an ``artists`` row (``SELECT *``) to the domain model; shared by both adapters."""
    outcome = row.get("mb_lookup_outcome")
    return Artist(
        id=row["id"],
        name=row["name"],
        sort_name=row["sort_name"],
        disambiguation=row.get("disambiguation"),
        needs_enhancement=row["needs_enhancement"],
        enhanced_at=row.get("enhanced_at"),
        enhancement_error=row.get("enhancement_error"),
        mbid=row.get("mbid"),
        origin=(CatalogSource(row["origin"]) if row.get("origin") else CatalogSource.LOCAL),
        normalized_name=row.get("normalized_name"),
        mb_lookup_at=row.get("mb_lookup_at"),
        mb_lookup_outcome=ArtistLinkOutcome(outcome) if outcome else None,
    )


class PgArtistRepository(ArtistCatalogRepository, ArtistEnhancementRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> Artist:
        return artist_from_row(row)

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
        row = self._conn.execute("SELECT * FROM artists WHERE id = %s", (artist.id,)).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_id(self, artist_id: str) -> Artist | None:
        row = self._conn.execute("SELECT * FROM artists WHERE id = %s", (artist_id,)).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_mbid(self, mbid: str) -> Artist | None:
        row = self._conn.execute("SELECT * FROM artists WHERE mbid = %s", (mbid,)).fetchone()
        return self._row_to_model(row) if row else None

    def create_stub(self, mbid: str) -> Artist:
        # ON CONFLICT: two writers resolving one unknown MBID at once (two Resolve presses, or
        # Resolve during a matching run) both end on the one stub, as ON CONFLICT let both
        # matches rows succeed before 1a.
        row = self._conn.execute(
            """INSERT INTO artists
                   (id, name, sort_name, normalized_name, mbid, origin, needs_enhancement)
               VALUES (gen_random_uuid()::text, %(mbid)s, %(mbid)s, NULL, %(mbid)s,
                       'musicbrainz', TRUE)
               ON CONFLICT (mbid) DO NOTHING
               RETURNING *""",
            {"mbid": mbid},
        ).fetchone()
        if row is None:
            row = self._conn.execute("SELECT * FROM artists WHERE mbid = %s", (mbid,)).fetchone()
        if row is None:
            raise RuntimeError(f"Stub artist not created: {mbid}")
        return self._row_to_model(row)

    def list_all(self) -> list[Artist]:
        # An unnamed stub (phase 1a S15) is no matching candidate: its name is its MBID.
        rows = self._conn.execute(
            "SELECT * FROM artists WHERE name IS DISTINCT FROM mbid ORDER BY name"
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
        held = self._conn.execute(
            "SELECT id FROM artists WHERE normalized_name = %s",
            (normalized_name,),
        ).fetchall()
        if len(held) == 1 and len(self.name_mbids(normalized_name)) <= 1:
            return cast(str, held[0]["id"])
        return self._name_only_artist(name, normalized_name)

    def _name_only_artist(self, name: str, normalized_name: str) -> str:
        """The one name-only artist of the name (C41), created if absent."""
        row = self._conn.execute(
            """INSERT INTO artists
                   (id, name, sort_name, normalized_name,
                    origin, needs_enhancement)
               VALUES (%s, %s, %s, %s, 'local', FALSE)
               ON CONFLICT (normalized_name) WHERE mbid IS NULL DO NOTHING
               RETURNING id""",
            (str(uuid4()), name, name, normalized_name),
        ).fetchone()
        if row is not None:
            add_current_name(self._conn, row["id"], ArtistNameSource.TAG)
            return cast(str, row["id"])
        row = self._conn.execute(
            "SELECT id FROM artists WHERE normalized_name = %s AND mbid IS NULL",
            (normalized_name,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Artist not found after ON CONFLICT: {normalized_name}")
        return cast(str, row["id"])

    def name_mbids(self, normalized_name: str) -> set[str]:
        rows = self._conn.execute(
            """SELECT DISTINCT artist_mbid FROM library_files
               WHERE normalized_artist_name = %s
                 AND file_status = 'present'
                 AND artist_mbid IS NOT NULL""",
            (normalized_name,),
        ).fetchall()
        return name_mbids_of(cast(str, row["artist_mbid"]) for row in rows)

    def upsert_musicbrainz_artist(
        self,
        mbid: str,
        name: str,
        sort_name: str,
        normalized_name: str,
        disambiguation: str | None = None,
        *,
        name_source: ArtistNameSource = ArtistNameSource.MUSICBRAINZ_NAME,
    ) -> str:
        return self._upsert_mb(
            mbid,
            (name, sort_name, normalized_name, disambiguation),
            name_source,
            lambda held: held <= {mbid},
        )

    def upsert_further_credit(
        self,
        mbid: str,
        name: str,
        sort_name: str,
        normalized_name: str,
        disambiguation: str | None = None,
    ) -> str:
        return self._upsert_mb(
            mbid,
            (name, sort_name, normalized_name, disambiguation),
            ArtistNameSource.MUSICBRAINZ_NAME,
            lambda held: held == {mbid},
        )

    def _upsert_mb(
        self,
        mbid: str,
        fields: tuple[str, str, str, str | None],
        name_source: ArtistNameSource,
        promotes: Callable[[set[str]], bool],
    ) -> str:
        """Find by MBID, else promote when *promotes* allows it given the name's MBIDs, else insert.
        *fields* is the name, sort name, normalized name and disambiguation."""
        name, sort_name, normalized_name, disambiguation = fields
        # 1. Return early if this exact MBID is already known.
        row = self._conn.execute("SELECT id FROM artists WHERE mbid = %s", (mbid,)).fetchone()
        if row is not None:
            return cast(str, row["id"])

        # 2. The name's MBIDs allow it (D5): the name-only artist takes it.
        row = None
        if promotes(self.name_mbids(normalized_name)):
            row = self._conn.execute(
                """UPDATE artists SET
                     mbid = %s,
                     origin = 'musicbrainz',
                     name = %s,
                     sort_name = %s,
                     disambiguation = %s,
                     needs_enhancement = TRUE
                   WHERE normalized_name = %s AND mbid IS NULL
                   RETURNING id""",
                (mbid, name, sort_name, disambiguation, normalized_name),
            ).fetchone()

        # 3. Otherwise a new artist, never another MBID's. A partial index can't arbitrate a row
        # that has an MBID, so only the MBID's own index does: a writer that got there first wins.
        if row is None:
            row = self._conn.execute(
                """INSERT INTO artists
                       (id, name, sort_name, normalized_name, mbid,
                        origin, needs_enhancement, disambiguation)
                   VALUES (%s, %s, %s, %s, %s, 'musicbrainz', TRUE, %s)
                   ON CONFLICT (mbid) DO NOTHING
                   RETURNING id""",
                (str(uuid4()), name, sort_name, normalized_name, mbid, disambiguation),
            ).fetchone()
        if row is None:
            row = self._conn.execute("SELECT id FROM artists WHERE mbid = %s", (mbid,)).fetchone()
        if row is None:
            raise RuntimeError(f"Artist upsert_musicbrainz_artist failed: {mbid}")
        _maybe_mark_complete_on_upsert(self._conn, row["id"])
        add_current_name(self._conn, row["id"], name_source)
        return cast(str, row["id"])

    def list_named_stubs_without_normalized_name(self) -> list[Artist]:
        rows = self._conn.execute(
            """SELECT * FROM artists
                WHERE mbid IS NOT NULL AND normalized_name IS NULL AND name <> mbid"""
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def set_normalized_name(self, artist_id: str, normalized_name: str) -> None:
        self._conn.execute(
            "UPDATE artists SET normalized_name = %s WHERE id = %s", (normalized_name, artist_id)
        )

    def get_by_normalized_name(
        self,
        normalized_name: str,
    ) -> Artist | None:
        row = self._conn.execute(
            "SELECT * FROM artists WHERE normalized_name = %s AND mbid IS NULL",
            (normalized_name,),
        ).fetchone()
        return self._row_to_model(row) if row else None


def _maybe_mark_complete_on_upsert(conn: psycopg.Connection[Any], artist_id: str) -> None:
    """Flip needs_enhancement back to FALSE if all completeness fields are filled.

    The `sort_name != name` guard aligns with the Tier 2/3 "would we update
    sort_name?" check in `_enhance_artist` — an artist whose sort_name merely
    echoes `name` is still a bare default and should go through MB lookup to
    pick up the real sort form (e.g., "Madonna" → "Ciccone, Madonna").

    Scope: only called from the promote and INSERT paths in `upsert_musicbrainz_artist`.
    Re-upserts of an already-known MBID short-circuit at the first SELECT
    and never reach this helper — the enhancement task handles those.
    """
    conn.execute(
        """UPDATE artists
           SET needs_enhancement = FALSE, enhanced_at = now()
           WHERE id = %s
             AND mbid IS NOT NULL
             AND disambiguation IS NOT NULL
             AND disambiguation != ''
             AND sort_name IS NOT NULL
             AND sort_name != ''
             AND sort_name != name""",
        (artist_id,),
    )
