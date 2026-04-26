from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.db.repositories._pg_utils import format_embedding, parse_embedding
from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import MatchStatus, ReasonCode
from backend.repositories.broadcast_artists import BroadcastArtistRepository


class PgBroadcastArtistRepository(BroadcastArtistRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> BroadcastArtist:
        rc = row.get("reason_code")
        return BroadcastArtist(
            id=row["id"],
            original_name=row["original_name"],
            normalized_name=row["normalized_name"],
            match_status=MatchStatus(row["match_status"]),
            artist_candidates=row.get("artist_candidates"),
            error_message=row.get("error_message"),
            created_at=row["created_at"],
            embedding=parse_embedding(row.get("embedding")),
            reason_code=ReasonCode(rc) if rc else None,
            reason_detail=row.get("reason_detail"),
        )

    def upsert(self, artist: BroadcastArtist) -> BroadcastArtist:
        self._conn.execute(
            """INSERT INTO broadcast_artists
               (id, original_name, normalized_name, match_status)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (normalized_name) DO NOTHING""",
            (artist.id, artist.original_name, artist.normalized_name,
             artist.match_status.value),
        )
        row = self._conn.execute(
            "SELECT * FROM broadcast_artists WHERE normalized_name = %s",
            (artist.normalized_name,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_id(self, artist_id: UUID) -> BroadcastArtist | None:
        row = self._conn.execute(
            "SELECT * FROM broadcast_artists WHERE id = %s", (artist_id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_ids(self, ids: list[UUID]) -> list[BroadcastArtist]:
        if not ids:
            return []
        rows = self._conn.execute(
            "SELECT * FROM broadcast_artists WHERE id = ANY(%s)",
            (ids,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_by_normalized_name(self, normalized_name: str) -> BroadcastArtist | None:
        row = self._conn.execute(
            "SELECT * FROM broadcast_artists WHERE normalized_name = %s",
            (normalized_name,),
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_all_for_playlist(self, playlist_id: UUID) -> list[BroadcastArtist]:
        rows = self._conn.execute(
            """SELECT DISTINCT la.* FROM broadcast_artists la
               JOIN track_identities li ON li.broadcast_artist_id = la.id
               JOIN play_events le ON le.identity_id = li.id
               WHERE le.playlist_id = %s""",
            (playlist_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_pending_for_playlist(self, playlist_id: UUID) -> list[BroadcastArtist]:
        rows = self._conn.execute(
            """SELECT DISTINCT la.* FROM broadcast_artists la
               JOIN track_identities li ON li.broadcast_artist_id = la.id
               JOIN play_events le ON le.identity_id = li.id
               WHERE le.playlist_id = %s AND la.match_status = %s""",
            (playlist_id, MatchStatus.PENDING.value),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_unembedded_for_playlist(
        self, playlist_id: UUID
    ) -> list[BroadcastArtist]:
        rows = self._conn.execute(
            """SELECT DISTINCT la.* FROM broadcast_artists la
               JOIN track_identities li ON li.broadcast_artist_id = la.id
               JOIN play_events le ON le.identity_id = li.id
               WHERE le.playlist_id = %s AND la.embedding IS NULL""",
            (playlist_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def update_match_status(
        self,
        artist_id: UUID,
        status: MatchStatus,
        reason_code: ReasonCode | None = None,
        reason_detail: str | None = None,
    ) -> None:
        self._conn.execute(
            """UPDATE broadcast_artists
               SET match_status = %s, reason_code = %s, reason_detail = %s
               WHERE id = %s""",
            (status.value, reason_code, reason_detail, artist_id),
        )

    def update_embedding(self, artist_id: UUID, embedding: list[float]) -> None:
        self._conn.execute(
            "UPDATE broadcast_artists SET embedding = %s WHERE id = %s",
            (format_embedding(embedding), artist_id),
        )

    def reset_deferred_by_ids(self, artist_ids: list[UUID]) -> int:
        if not artist_ids:
            return 0
        cur = self._conn.execute(
            """UPDATE broadcast_artists
               SET match_status = %s, reason_code = NULL, reason_detail = NULL
               WHERE id = ANY(%s)
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
