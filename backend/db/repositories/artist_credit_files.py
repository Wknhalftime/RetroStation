from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.catalog import SPECIAL_PURPOSE_MBIDS
from backend.domain.library import (
    ArtistCreditFile,
    CreditEntry,
    CreditGroup,
    LibraryStorageError,
)
from backend.repositories.artist_credit_files import ArtistCreditFilesRepository


def credit_json(credit: Sequence[CreditEntry] | None) -> Jsonb | None:
    """A credit as the column holds it, an ordered list of ``{name, mbid, join}``; NULL for none."""
    if credit is None:
        return None
    return Jsonb([{"name": e.name, "mbid": e.mbid, "join": e.join} for e in credit])


def _tags_of(raw: dict[str, Any] | str) -> dict[str, Any]:
    """As ``PgLibraryFileRepository`` reads it: a row can hold its JSON as a string."""
    return cast(dict[str, Any], json.loads(raw)) if isinstance(raw, str) else raw


@translate_lost_connection(LibraryStorageError)
class PgArtistCreditFilesRepository(ArtistCreditFilesRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def list_to_fill(self, after_id: UUID | None, limit: int) -> list[ArtistCreditFile]:
        rows = self._conn.execute(
            """SELECT id, file_path, format, raw_metadata, recording_mbid
                 FROM library_files
                WHERE raw_metadata IS NOT NULL
                  AND (artist_mbids IS NULL OR credit @> '[{"name": null}]')
                  AND (%(after)s::uuid IS NULL OR id > %(after)s::uuid)
                ORDER BY id
                LIMIT %(limit)s""",
            {"after": after_id, "limit": limit},
        ).fetchall()
        return [
            ArtistCreditFile(
                id=row["id"],
                file_path=row["file_path"],
                format=row["format"],
                raw_metadata=_tags_of(row["raw_metadata"]),
                recording_mbid=row["recording_mbid"],
            )
            for row in rows
        ]

    def write_credit(
        self, file_id: UUID, artist_mbids: Sequence[str], credit: Sequence[CreditEntry] | None
    ) -> None:
        self._conn.execute(
            "UPDATE library_files SET artist_mbids = %s, credit = %s WHERE id = %s",
            (list(artist_mbids), credit_json(credit), file_id),
        )

    def list_credit_groups(self) -> list[CreditGroup]:
        rows = self._conn.execute(
            """SELECT entry->>'mbid' AS mbid, entry->>'name' AS name, count(*) AS files
                 FROM library_files, jsonb_array_elements(credit) AS entry
                WHERE file_status = 'present'
                  AND cardinality(artist_mbids) > 1
                  AND (SELECT count(*) FROM unnest(artist_mbids) AS real(mbid)
                        WHERE real.mbid <> ALL(%(placeholders)s)) > 1
                GROUP BY 1, 2""",
            {"placeholders": list(SPECIAL_PURPOSE_MBIDS)},
        ).fetchall()
        return [CreditGroup(r["mbid"], r["name"], r["files"]) for r in rows]

    def list_tag_spellings(self) -> set[tuple[str, str]]:
        rows = self._conn.execute(
            """SELECT DISTINCT entry->>'mbid' AS mbid, entry->>'name' AS name
                 FROM library_files, jsonb_array_elements(credit) AS entry
                WHERE entry->>'name' IS NOT NULL"""
        ).fetchall()
        return {(r["mbid"], r["name"]) for r in rows}
