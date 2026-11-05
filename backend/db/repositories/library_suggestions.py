"""PostgreSQL adapter for ``library_suggestions`` (migration 0047; phase 2a S13, S14; C21). Writes
in the caller's transaction and commits nothing (C75)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.enums import SuggestionReason, SuggestionState
from backend.domain.library import LibraryStorageError, LibrarySuggestion
from backend.repositories.library_suggestions import LibrarySuggestionRepository


@translate_lost_connection(LibraryStorageError)
class PgLibrarySuggestionRepository(LibrarySuggestionRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def upsert_for_name(
        self,
        normalized_name: str,
        reason: SuggestionReason,
        candidates: Sequence[str],
        evidence: Mapping[str, object],
    ) -> bool:
        cur = self._conn.execute(
            """INSERT INTO library_suggestions (normalized_name, reason, candidates, evidence)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (normalized_name, reason) WHERE library_file_id IS NULL
               DO UPDATE SET candidates = EXCLUDED.candidates,
                             evidence = EXCLUDED.evidence,
                             updated_at = now()
               WHERE (library_suggestions.candidates, library_suggestions.evidence)
                     IS DISTINCT FROM (EXCLUDED.candidates, EXCLUDED.evidence)""",
            (normalized_name, reason.value, Jsonb(list(candidates)), Jsonb(dict(evidence))),
        )
        return cur.rowcount == 1

    def get_for_name(
        self, normalized_name: str, reason: SuggestionReason
    ) -> LibrarySuggestion | None:
        row = self._conn.execute(
            """SELECT candidates, evidence, state FROM library_suggestions
                WHERE normalized_name = %s AND reason = %s AND library_file_id IS NULL""",
            (normalized_name, reason.value),
        ).fetchone()
        if row is None:
            return None
        return LibrarySuggestion(
            normalized_name=normalized_name,
            reason=reason,
            candidates=tuple(row["candidates"]),
            evidence=row["evidence"],
            state=SuggestionState(row["state"]),
        )
