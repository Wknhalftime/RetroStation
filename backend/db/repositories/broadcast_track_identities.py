from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.db.repositories._pg_utils import format_embedding, parse_embedding
from backend.domain.broadcast import BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, MatchTier
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.services.matching_reasons import ReasonCode


class PgBroadcastTrackIdentityRepository(BroadcastTrackIdentityRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> BroadcastTrackIdentity:
        return BroadcastTrackIdentity(
            id=row["id"],
            broadcast_artist_id=row["broadcast_artist_id"],
            original_title=row["original_title"],
            normalized_title=row["normalized_title"],
            normalized_signature=row["normalized_signature"],
            match_status=MatchStatus(row["match_status"]),
            match_tier=(
                MatchTier(row["match_tier"]) if row.get("match_tier") else None
            ),
            created_at=row["created_at"],
            embedding=parse_embedding(row.get("embedding")),
        )

    def upsert(self, identity: BroadcastTrackIdentity) -> BroadcastTrackIdentity:
        self._conn.execute(
            """INSERT INTO track_identities
               (id, broadcast_artist_id, original_title, normalized_title,
                normalized_signature, match_status)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (normalized_signature) DO NOTHING""",
            (identity.id, identity.broadcast_artist_id,
             identity.original_title, identity.normalized_title,
             identity.normalized_signature,
             identity.match_status.value),
        )
        row = self._conn.execute(
            "SELECT * FROM track_identities WHERE normalized_signature = %s",
            (identity.normalized_signature,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_id(self, identity_id: UUID) -> BroadcastTrackIdentity | None:
        row = self._conn.execute(
            "SELECT * FROM track_identities WHERE id = %s", (identity_id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_signature(
        self, normalized_signature: str
    ) -> BroadcastTrackIdentity | None:
        row = self._conn.execute(
            "SELECT * FROM track_identities WHERE normalized_signature = %s",
            (normalized_signature,),
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_for_artist(
        self, broadcast_artist_id: UUID
    ) -> list[BroadcastTrackIdentity]:
        rows = self._conn.execute(
            "SELECT * FROM track_identities WHERE broadcast_artist_id = %s",
            (broadcast_artist_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_pending_for_playlist(
        self, playlist_id: UUID
    ) -> list[BroadcastTrackIdentity]:
        rows = self._conn.execute(
            """SELECT DISTINCT li.* FROM track_identities li
               JOIN play_events le ON le.identity_id = li.id
               JOIN broadcast_artists la
                   ON la.id = li.broadcast_artist_id
               WHERE le.playlist_id = %s
                 AND li.match_status = %s
                 AND la.match_status IN (%s, %s)""",
            (playlist_id, MatchStatus.PENDING.value,
             MatchStatus.AUTO_MATCHED.value,
             MatchStatus.MANUAL_MATCHED.value),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_unembedded_for_playlist(
        self, playlist_id: UUID
    ) -> list[BroadcastTrackIdentity]:
        rows = self._conn.execute(
            """SELECT DISTINCT li.* FROM track_identities li
               JOIN play_events le ON le.identity_id = li.id
               WHERE le.playlist_id = %s AND li.embedding IS NULL""",
            (playlist_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def update_match_status(
        self,
        identity_id: UUID,
        status: MatchStatus,
        tier: MatchTier | None = None,
        reason_code: ReasonCode | None = None,
        reason_detail: str | None = None,
    ) -> None:
        self._conn.execute(
            """UPDATE track_identities
               SET match_status = %s, match_tier = %s,
                   reason_code = %s, reason_detail = %s
               WHERE id = %s""",
            (status.value, tier.value if tier is not None else None,
             reason_code, reason_detail, identity_id),
        )

    def update_embedding(self, identity_id: UUID, embedding: list[float]) -> None:
        self._conn.execute(
            "UPDATE track_identities SET embedding = %s WHERE id = %s",
            (format_embedding(embedding), identity_id),
        )

    def bulk_reject_by_artist(self, broadcast_artist_id: UUID) -> None:
        self._conn.execute(
            """UPDATE track_identities
               SET match_status = %s, match_tier = %s
               WHERE broadcast_artist_id = %s AND match_status = %s""",
            (MatchStatus.AUTO_REJECTED.value, MatchTier.UNCLASSIFIED.value,
             broadcast_artist_id, MatchStatus.PENDING.value),
        )

