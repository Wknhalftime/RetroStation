from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.domain.models import FormatOverride
from backend.repositories.format_overrides import FormatOverrideRepository


class PgFormatOverrideRepository(FormatOverrideRepository):
    """PostgreSQL implementation of :class:`FormatOverrideRepository`."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> FormatOverride:
        return FormatOverride(
            id=row["id"],
            work_id=row["work_id"],
            format_name=row["format_name"],
            preferred_file_id=row["preferred_file_id"],
            notes=row.get("notes"),
            created_at=row["created_at"],
        )

    def create(self, override: FormatOverride) -> FormatOverride:
        """Insert a new format override row.

        Args:
            override: The :class:`FormatOverride` to persist.

        Returns:
            The persisted model (with DB-generated ``created_at`` if not set).
        """
        self._conn.execute(
            """
            INSERT INTO format_overrides
                (id, work_id, format_name, preferred_file_id, notes)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                override.id,
                override.work_id,
                override.format_name,
                override.preferred_file_id,
                override.notes,
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM format_overrides WHERE id = %s", (override.id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get(self, work_id: str, format_name: str) -> FormatOverride | None:
        """Fetch a format override by (work_id, format_name) unique key.

        Args:
            work_id: The work MBID.
            format_name: The format string (e.g. ``"flac"``).

        Returns:
            The matching :class:`FormatOverride`, or ``None`` if absent.
        """
        row = self._conn.execute(
            "SELECT * FROM format_overrides WHERE work_id = %s AND format_name = %s",
            (work_id, format_name),
        ).fetchone()
        return self._row_to_model(row) if row else None

    def list_by_work(self, work_id: str) -> list[FormatOverride]:
        """Return all format overrides for a given work, ordered by format_name.

        Args:
            work_id: The work MBID.

        Returns:
            List of :class:`FormatOverride` instances.
        """
        rows = self._conn.execute(
            "SELECT * FROM format_overrides WHERE work_id = %s ORDER BY format_name",
            (work_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def delete(self, id: UUID) -> None:
        """Delete a format override by primary key.

        Args:
            id: The UUID of the override to remove.
        """
        self._conn.execute(
            "DELETE FROM format_overrides WHERE id = %s", (id,)
        )
