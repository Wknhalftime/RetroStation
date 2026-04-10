from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.domain.enums import MatchStatus, MatchTier
from backend.domain.models import LogIdentity
from backend.repositories.log_identities import LogIdentityRepository


def _parse_embedding(raw: Any) -> list[float] | None:
    """Convert a pgvector embedding column value to a list of floats.

    pgvector may return the value as a Python list (already parsed) or as a
    bracketed string such as ``"[0.1,0.2,0.3]"``.  Both cases are handled
    gracefully; ``None`` is returned when the column is NULL.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    return [float(x) for x in str(raw).strip("[]").split(",")]


class PgLogIdentityRepository(LogIdentityRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> LogIdentity:
        return LogIdentity(
            id=row["id"],
            artist_id=row["artist_id"],
            original_title=row["original_title"],
            normalized_title=row["normalized_title"],
            normalized_signature=row["normalized_signature"],
            match_status=MatchStatus(row["match_status"]),
            match_tier=MatchTier(row["match_tier"]) if row.get("match_tier") else None,
            created_at=row["created_at"],
            embedding=_parse_embedding(row.get("embedding")),
        )

    def upsert(self, identity: LogIdentity) -> LogIdentity:
        self._conn.execute(
            """INSERT INTO log_identities
               (id, artist_id, original_title, normalized_title, normalized_signature, match_status)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (normalized_signature) DO NOTHING""",
            (identity.id, identity.artist_id, identity.original_title,
             identity.normalized_title, identity.normalized_signature,
             identity.match_status.value),
        )
        row = self._conn.execute(
            "SELECT * FROM log_identities WHERE normalized_signature = %s",
            (identity.normalized_signature,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_id(self, id: UUID) -> LogIdentity | None:
        row = self._conn.execute(
            "SELECT * FROM log_identities WHERE id = %s", (id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_signature(self, normalized_signature: str) -> LogIdentity | None:
        row = self._conn.execute(
            "SELECT * FROM log_identities WHERE normalized_signature = %s",
            (normalized_signature,),
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_for_artist(self, artist_id: UUID) -> list[LogIdentity]:
        rows = self._conn.execute(
            "SELECT * FROM log_identities WHERE artist_id = %s",
            (artist_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_pending_for_playlist(self, playlist_id: UUID) -> list[LogIdentity]:
        rows = self._conn.execute(
            """SELECT DISTINCT li.* FROM log_identities li
               JOIN log_events le ON le.identity_id = li.id
               JOIN log_artists la ON la.id = li.artist_id
               WHERE le.playlist_id = %s
                 AND li.match_status = %s
                 AND la.match_status IN (%s, %s)""",
            (playlist_id, MatchStatus.PENDING.value,
             MatchStatus.AUTO_MATCHED.value, MatchStatus.MANUAL_MATCHED.value),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_unembedded_for_playlist(self, playlist_id: UUID) -> list[LogIdentity]:
        rows = self._conn.execute(
            """SELECT DISTINCT li.* FROM log_identities li
               JOIN log_events le ON le.identity_id = li.id
               WHERE le.playlist_id = %s AND li.embedding IS NULL""",
            (playlist_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def update_match_status(
        self, id: UUID, status: MatchStatus, tier: MatchTier
    ) -> None:
        self._conn.execute(
            "UPDATE log_identities SET match_status = %s, match_tier = %s WHERE id = %s",
            (status.value, tier.value, id),
        )

    def update_embedding(self, id: UUID, embedding: list[float]) -> None:
        self._conn.execute(
            "UPDATE log_identities SET embedding = %s WHERE id = %s",
            ("[" + ",".join(str(v) for v in embedding) + "]", id),
        )

    def bulk_reject_by_artist(self, artist_id: UUID) -> None:
        self._conn.execute(
            """UPDATE log_identities
               SET match_status = %s, match_tier = %s
               WHERE artist_id = %s AND match_status = %s""",
            (MatchStatus.AUTO_REJECTED.value, MatchTier.UNKNOWN.value,
             artist_id, MatchStatus.PENDING.value),
        )
