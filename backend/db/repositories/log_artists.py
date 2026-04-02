from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.domain.enums import MatchStatus, MatchTier
from backend.domain.models import LogArtist
from backend.repositories.log_artists import LogArtistRepository


class PgLogArtistRepository(LogArtistRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> LogArtist:
        return LogArtist(
            id=row["id"],
            original_name=row["original_name"],
            normalized_name=row["normalized_name"],
            match_status=MatchStatus(row["match_status"]),
            artist_candidates=row.get("artist_candidates"),
            error_message=row.get("error_message"),
            created_at=row["created_at"],
            embedding=(
                [float(x) for x in row["embedding"].strip("[]").split(",")]
                if row.get("embedding")
                else None
            ),
        )

    def upsert(self, artist: LogArtist) -> LogArtist:
        self._conn.execute(
            """INSERT INTO log_artists (id, original_name, normalized_name, match_status)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (normalized_name) DO NOTHING""",
            (artist.id, artist.original_name, artist.normalized_name,
             artist.match_status.value),
        )
        row = self._conn.execute(
            "SELECT * FROM log_artists WHERE normalized_name = %s",
            (artist.normalized_name,),
        ).fetchone()
        assert row is not None
        return self._row_to_model(row)

    def get_by_id(self, id: UUID) -> LogArtist | None:
        row = self._conn.execute(
            "SELECT * FROM log_artists WHERE id = %s", (id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_normalized_name(self, normalized_name: str) -> LogArtist | None:
        row = self._conn.execute(
            "SELECT * FROM log_artists WHERE normalized_name = %s",
            (normalized_name,),
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_pending_for_playlist(self, playlist_id: UUID) -> list[LogArtist]:
        rows = self._conn.execute(
            """SELECT DISTINCT la.* FROM log_artists la
               JOIN log_identities li ON li.artist_id = la.id
               JOIN log_events le ON le.identity_id = li.id
               WHERE le.playlist_id = %s AND la.match_status = %s""",
            (playlist_id, MatchStatus.PENDING.value),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def get_unembedded_for_playlist(self, playlist_id: UUID) -> list[LogArtist]:
        rows = self._conn.execute(
            """SELECT DISTINCT la.* FROM log_artists la
               JOIN log_identities li ON li.artist_id = la.id
               JOIN log_events le ON le.identity_id = li.id
               WHERE le.playlist_id = %s AND la.embedding IS NULL""",
            (playlist_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def update_match_status(
        self, id: UUID, status: MatchStatus, tier: MatchTier | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE log_artists SET match_status = %s WHERE id = %s",
            (status.value, id),
        )

    def update_embedding(self, id: UUID, embedding: list[float]) -> None:
        self._conn.execute(
            "UPDATE log_artists SET embedding = %s WHERE id = %s",
            ("[" + ",".join(str(v) for v in embedding) + "]", id),
        )
