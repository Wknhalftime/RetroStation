from __future__ import annotations

from typing import Any

import psycopg

from backend.domain.library import LibraryQuarantine
from backend.repositories.library_quarantine import LibraryQuarantineRepository


class PgLibraryQuarantineRepository(LibraryQuarantineRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> LibraryQuarantine:
        return LibraryQuarantine(
            id=row["id"],
            file_path=row["file_path"],
            error_message=row["error_message"],
            trace_id=row.get("trace_id"),
            created_at=row["created_at"],
        )

    def create(self, entry: LibraryQuarantine) -> LibraryQuarantine:
        self._conn.execute(
            """INSERT INTO library_quarantine (id, file_path, error_message, trace_id)
               VALUES (%s, %s, %s, %s)""",
            (entry.id, entry.file_path, entry.error_message, entry.trace_id),
        )
        row = self._conn.execute(
            "SELECT * FROM library_quarantine WHERE id = %s",
            (entry.id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def create_write_only(self, entry: LibraryQuarantine) -> None:
        """Insert a quarantine entry without reading back the row."""
        self._conn.execute(
            """INSERT INTO library_quarantine (id, file_path, error_message, trace_id)
               VALUES (%s, %s, %s, %s)""",
            (entry.id, entry.file_path, entry.error_message, entry.trace_id),
        )

    def list_all(self) -> list[LibraryQuarantine]:
        rows = self._conn.execute(
            "SELECT * FROM library_quarantine ORDER BY created_at"
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_by_path(self, file_path: str) -> LibraryQuarantine | None:
        row = self._conn.execute(
            "SELECT * FROM library_quarantine WHERE file_path = %s",
            (file_path,),
        ).fetchone()
        return self._row_to_model(row) if row else None
