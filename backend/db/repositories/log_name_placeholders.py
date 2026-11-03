"""PostgreSQL adapter for the not-an-artist list, ``log_name_placeholders`` (migration 0045; C7,
phase 1c S6). Writes in the caller's transaction and commits nothing (C75)."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.enums import PlaceholderSource
from backend.domain.matching import MatchingStorageError
from backend.repositories.log_name_placeholders import LogNamePlaceholderRepository


@translate_lost_connection(MatchingStorageError)
class PgLogNamePlaceholderRepository(LogNamePlaceholderRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def listed(self, normalized_names: Collection[str]) -> set[str]:
        if not normalized_names:
            return set()
        rows = self._conn.execute(
            "SELECT normalized_name FROM log_name_placeholders WHERE normalized_name = ANY(%s)",
            (list(normalized_names),),
        ).fetchall()
        return {row["normalized_name"] for row in rows}

    def add(self, normalized_name: str, added_by: PlaceholderSource) -> bool:
        cur = self._conn.execute(
            """INSERT INTO log_name_placeholders (normalized_name, added_by) VALUES (%s, %s)
               ON CONFLICT (normalized_name) DO NOTHING""",
            (normalized_name, added_by.value),
        )
        return cur.rowcount == 1

    def remove(self, normalized_name: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM log_name_placeholders WHERE normalized_name = %s", (normalized_name,)
        )
        return cur.rowcount == 1
