"""PostgreSQL adapter for the whole-name Reject and Unmatch cascade (phase 1b, M24)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.artist_cascade import CascadeLine
from backend.domain.enums import MatchStatus, ReasonCode
from backend.domain.matching import MatchingStorageError
from backend.repositories.artist_cascade import ArtistCascadeRepository

# One query (SPEC 1b, Limits): each line with its plays and the file of the match it plays,
# picked as play_file_resolution picks it (migration 0030).
_LINES_SQL = """
SELECT ti.id,
       ti.match_status,
       (SELECT count(*) FROM play_events pe WHERE pe.identity_id = ti.id) AS plays,
       COALESCE(f.normalized_artist_name = ba.normalized_name, FALSE) AS same_name
  FROM track_identities ti
  JOIN broadcast_artists ba ON ba.id = ti.broadcast_artist_id
  LEFT JOIN LATERAL (
      SELECT m.library_file_id
        FROM matches m
       WHERE m.identity_id = ti.id
         AND m.library_file_id IS NOT NULL
       ORDER BY m.confidence_score DESC, m.created_at, m.id
       LIMIT 1
  ) best ON true
  LEFT JOIN library_files f ON f.id = best.library_file_id
 WHERE ti.broadcast_artist_id = %s
 ORDER BY ti.id
"""


@translate_lost_connection(MatchingStorageError)
class PgArtistCascadeRepository(ArtistCascadeRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def lines_of(self, broadcast_artist_id: UUID, *, lock: bool = False) -> list[CascadeLine]:
        if lock:
            # The aggregate read cannot take FOR UPDATE; lock the rows first, in id order.
            self._conn.execute(
                """SELECT id FROM track_identities WHERE broadcast_artist_id = %s
                    ORDER BY id FOR UPDATE""",
                (broadcast_artist_id,),
            )
        rows = self._conn.execute(_LINES_SQL, (broadcast_artist_id,)).fetchall()
        return [
            CascadeLine(
                identity_id=row["id"],
                status=MatchStatus(row["match_status"]),
                same_name=bool(row["same_name"]),
                plays=int(row["plays"]),
            )
            for row in rows
        ]

    def reject_lines(self, identity_ids: Sequence[UUID]) -> None:
        if not identity_ids:
            return
        self._conn.execute(
            "UPDATE track_identities SET match_status = %s WHERE id = ANY(%s)",
            (MatchStatus.AUTO_REJECTED.value, list(identity_ids)),
        )

    def reopen_lines(self, identity_ids: Sequence[UUID]) -> None:
        if not identity_ids:
            return
        self._conn.execute(
            """
            UPDATE track_identities
               SET match_status  = %s,
                   match_tier    = NULL,
                   reason_code   = %s,
                   reason_detail = NULL
             WHERE id = ANY(%s)
            """,
            (MatchStatus.NEEDS_REVIEW.value, ReasonCode.USER_UNMATCHED.value, list(identity_ids)),
        )

    def delete_matches_of_lines(self, identity_ids: Sequence[UUID]) -> None:
        if not identity_ids:
            return
        self._conn.execute("DELETE FROM matches WHERE identity_id = ANY(%s)", (list(identity_ids),))
