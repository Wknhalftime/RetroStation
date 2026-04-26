from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.db.repositories._pg_utils import format_embedding, parse_embedding
from backend.domain.broadcast import BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, MatchTier, ReasonCode
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.services.matching_reasons import format_deferred_retry


class PgBroadcastTrackIdentityRepository(BroadcastTrackIdentityRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> BroadcastTrackIdentity:
        rc = row.get("reason_code")
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
            reason_code=ReasonCode(rc) if rc else None,
            reason_detail=row.get("reason_detail"),
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
        # No filter on broadcast_artists.match_status: BroadcastToLocalStrategy
        # handles the unresolved-artist path (artist PENDING/NEEDS_REVIEW) and
        # gating the SQL on AUTO/MANUAL artists would strand those identities
        # with no matching path ever running in production.
        rows = self._conn.execute(
            """SELECT DISTINCT li.* FROM track_identities li
               JOIN play_events le ON le.identity_id = li.id
               WHERE le.playlist_id = %s
                 AND li.match_status = %s""",
            (playlist_id, MatchStatus.PENDING.value),
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
        tier: MatchTier | None,
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
               SET match_status = %s, match_tier = %s,
                   reason_code = NULL, reason_detail = NULL
               WHERE broadcast_artist_id = %s AND match_status = %s""",
            (MatchStatus.AUTO_REJECTED.value, MatchTier.UNCLASSIFIED.value,
             broadcast_artist_id, MatchStatus.PENDING.value),
        )

    def bulk_defer_by_artist(self, broadcast_artist_id: UUID) -> int:
        cur = self._conn.execute(
            """UPDATE track_identities
               SET match_status = %s, match_tier = %s,
                   reason_code = %s, reason_detail = %s
               WHERE broadcast_artist_id = %s AND match_status = %s""",
            (
                MatchStatus.NEEDS_REVIEW.value,
                MatchTier.UNCLASSIFIED.value,
                ReasonCode.DEFERRED_RETRY.value,
                format_deferred_retry(),
                broadcast_artist_id,
                MatchStatus.PENDING.value,
            ),
        )
        return cur.rowcount

    def reset_deferred_by_artist_ids(self, artist_ids: list[UUID]) -> int:
        if not artist_ids:
            return 0
        # match_tier = NULL matches the initial state of freshly-ingested
        # PENDING rows (the column has no default and inserts omit it).
        # Setting UNCLASSIFIED here would drift telemetry/queries that
        # check tier on PENDING rows.
        cur = self._conn.execute(
            """UPDATE track_identities
               SET match_status = %s, match_tier = NULL,
                   reason_code = NULL, reason_detail = NULL
               WHERE broadcast_artist_id = ANY(%s)
                 AND match_status = %s
                 AND reason_code = %s""",
            (
                MatchStatus.PENDING.value,
                artist_ids,
                MatchStatus.NEEDS_REVIEW.value,
                ReasonCode.DEFERRED_RETRY.value,
            ),
        )
        return cur.rowcount

