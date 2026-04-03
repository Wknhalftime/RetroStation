from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.domain.enums import MatchTier, TargetType
from backend.domain.models import Match
from backend.repositories.matches import MatchRepository


class PgMatchRepository(MatchRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> Match:
        return Match(
            id=row["id"],
            confidence_score=row["confidence_score"],
            match_tier=MatchTier(row["match_tier"]),
            identity_id=row.get("identity_id"),
            artist_id=row.get("artist_id"),
            library_file_id=row.get("library_file_id"),
            target_id=row.get("target_id"),
            target_type=TargetType(row["target_type"]) if row.get("target_type") else None,
            trace_id=row.get("trace_id"),
            created_at=row["created_at"],
        )

    def create(self, match: Match) -> Match:
        self._conn.execute(
            """INSERT INTO matches
               (id, confidence_score, match_tier, identity_id, artist_id,
                library_file_id, target_id, target_type, trace_id, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (match.id, match.confidence_score, match.match_tier.value,
             match.identity_id, match.artist_id, match.library_file_id,
             match.target_id,
             match.target_type.value if match.target_type else None,
             match.trace_id, match.created_at),
        )
        row = self._conn.execute(
            "SELECT * FROM matches WHERE id = %s", (match.id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_identity(self, identity_id: UUID) -> Match | None:
        row = self._conn.execute(
            "SELECT * FROM matches WHERE identity_id = %s", (identity_id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_artist(self, artist_id: UUID) -> Match | None:
        row = self._conn.execute(
            "SELECT * FROM matches WHERE artist_id = %s", (artist_id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def delete_for_identity(self, identity_id: UUID) -> None:
        self._conn.execute(
            "DELETE FROM matches WHERE identity_id = %s", (identity_id,)
        )
