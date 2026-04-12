from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import MatchStatus, MatchTier
from backend.repositories.broadcast_artists import BroadcastArtistRepository


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


class PgBroadcastArtistRepository(BroadcastArtistRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> BroadcastArtist:
        return BroadcastArtist(
            id=row["id"],
            original_name=row["original_name"],
            normalized_name=row["normalized_name"],
            match_status=MatchStatus(row["match_status"]),
            artist_candidates=row.get("artist_candidates"),
            error_message=row.get("error_message"),
            created_at=row["created_at"],
            embedding=_parse_embedding(row.get("embedding")),
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

    def get_by_id(self, id: UUID) -> BroadcastArtist | None:
        row = self._conn.execute(
            "SELECT * FROM broadcast_artists WHERE id = %s", (id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

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
        self, id: UUID, status: MatchStatus, tier: MatchTier | None = None
    ) -> None:
        # broadcast_artists has no match_tier column (only track_identities does).
        # The tier parameter is accepted for ABC interface consistency but not stored.
        self._conn.execute(
            "UPDATE broadcast_artists SET match_status = %s WHERE id = %s",
            (status.value, id),
        )

    def update_embedding(self, id: UUID, embedding: list[float]) -> None:
        self._conn.execute(
            "UPDATE broadcast_artists SET embedding = %s WHERE id = %s",
            ("[" + ",".join(str(v) for v in embedding) + "]", id),
        )
