from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.broadcast import (
    BroadcastStation,
    BroadcastStorageError,
    DuplicateCallLettersError,
)
from backend.repositories.broadcast_stations import BroadcastStationRepository

#: Unique constraints a call-letters collision can violate: the original case-sensitive
#: column constraint, and the case-insensitive index added for D72. Either one, keyed by
#: name, means the same station already exists in some case; any other constraint name is
#: an unrelated unique violation and must not be mistaken for a duplicate (C1).
_CALL_LETTERS_CONSTRAINTS = frozenset(
    {"stations_call_letters_key", "idx_stations_call_letters_lower"}
)


@translate_lost_connection(BroadcastStorageError)
class PgBroadcastStationRepository(BroadcastStationRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> BroadcastStation:
        return BroadcastStation(
            id=row["id"],
            call_letters=row["call_letters"],
            name=row.get("name"),
            city=row.get("city"),
            format_name=row.get("format_name"),
            created_at=row["created_at"],
        )

    def create(self, station: BroadcastStation) -> BroadcastStation:
        try:
            self._conn.execute(
                """INSERT INTO stations (id, call_letters, name, city, format_name)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    station.id,
                    station.call_letters,
                    station.name,
                    station.city,
                    station.format_name,
                ),
            )
        except psycopg.errors.UniqueViolation as exc:
            if not self._is_call_letters_collision(exc):
                raise
            raise DuplicateCallLettersError(
                f"another station already has the call letters {station.call_letters!r}"
            ) from exc
        row = self._conn.execute("SELECT * FROM stations WHERE id = %s", (station.id,)).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_id(self, station_id: UUID) -> BroadcastStation | None:
        row = self._conn.execute("SELECT * FROM stations WHERE id = %s", (station_id,)).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_call_letters(self, call_letters: str) -> BroadcastStation | None:
        row = self._conn.execute(
            "SELECT * FROM stations WHERE lower(call_letters) = lower(%s)", (call_letters,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def _is_call_letters_collision(self, exc: psycopg.errors.UniqueViolation) -> bool:
        """True when ``exc`` is a call-letters collision (D72; C1: translation keyed by
        constraint name); false for any other unique violation, which the caller re-raises
        unchanged."""
        return exc.diag.constraint_name in _CALL_LETTERS_CONSTRAINTS

    def list_all(self) -> list[BroadcastStation]:
        rows = self._conn.execute("SELECT * FROM stations ORDER BY call_letters").fetchall()
        return [self._row_to_model(r) for r in rows]

    def update(self, station: BroadcastStation) -> BroadcastStation:
        try:
            self._conn.execute(
                """UPDATE stations
                   SET call_letters = %s, name = %s, city = %s, format_name = %s
                   WHERE id = %s""",
                (
                    station.call_letters,
                    station.name,
                    station.city,
                    station.format_name,
                    station.id,
                ),
            )
        except psycopg.errors.UniqueViolation as exc:
            if not self._is_call_letters_collision(exc):
                raise
            raise DuplicateCallLettersError(
                f"another station already has the call letters {station.call_letters!r}"
            ) from exc
        row = self._conn.execute("SELECT * FROM stations WHERE id = %s", (station.id,)).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def delete(self, station_id: UUID) -> None:
        self._conn.execute("DELETE FROM stations WHERE id = %s", (station_id,))
