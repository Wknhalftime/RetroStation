"""PostgreSQL adapter for the artist Resolve and Unmatch statements (phase 1a, C75): the
statements ``routers/matching.py`` ran on its async connection before, unchanged."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.enums import MatchStatus, ReasonCode
from backend.domain.matching import MatchingStorageError
from backend.repositories.artist_resolution import ArtistResolutionRepository

# Songs in the review queue, which a manual match sends back to the matcher.
_QUEUE_STATUSES = [MatchStatus.NEEDS_REVIEW.value, MatchStatus.PENDING.value]
# Songs a manual reject of their artist leaves alone.
_PROTECTED_STATUSES = [MatchStatus.MANUAL_MATCHED.value, MatchStatus.MANUAL_REJECTED.value]
# Artist statuses Unmatch reverts; pending and needs_review have nothing to revert.
_UNMATCHABLE_STATUSES = [
    MatchStatus.AUTO_MATCHED.value,
    MatchStatus.MANUAL_MATCHED.value,
    MatchStatus.AUTO_REJECTED.value,
    MatchStatus.MANUAL_REJECTED.value,
]


@translate_lost_connection(MatchingStorageError)
class PgArtistResolutionRepository(ArtistResolutionRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def exists(self, broadcast_artist_id: UUID) -> bool:
        row = self._conn.execute(
            "SELECT id FROM broadcast_artists WHERE id = %s", (broadcast_artist_id,)
        ).fetchone()
        return row is not None

    def status_of(self, broadcast_artist_id: UUID) -> MatchStatus | None:
        row = self._conn.execute(
            "SELECT match_status FROM broadcast_artists WHERE id = %s", (broadcast_artist_id,)
        ).fetchone()
        return None if row is None else MatchStatus(row["match_status"])

    def set_status(self, broadcast_artist_id: UUID, status: MatchStatus) -> None:
        self._conn.execute(
            "UPDATE broadcast_artists SET match_status = %s WHERE id = %s",
            (status.value, broadcast_artist_id),
        )

    def reopen_songs_in_review(self, broadcast_artist_id: UUID) -> None:
        self._conn.execute(
            """
            UPDATE track_identities
               SET match_status = %s,
                   match_tier   = NULL,
                   reason_code  = NULL,
                   reason_detail = NULL
             WHERE broadcast_artist_id = %s
               AND match_status = ANY(%s)
            """,
            (MatchStatus.PENDING.value, broadcast_artist_id, _QUEUE_STATUSES),
        )

    def delete_pending_song_matches(self, broadcast_artist_id: UUID) -> None:
        self._conn.execute(
            """
            DELETE FROM matches
             WHERE identity_id IN (
                 SELECT id FROM track_identities
                  WHERE broadcast_artist_id = %s
                    AND match_status = %s
             )
            """,
            (broadcast_artist_id, MatchStatus.PENDING.value),
        )

    def playlists_playing(self, broadcast_artist_id: UUID) -> list[UUID]:
        rows = self._conn.execute(
            """
            SELECT DISTINCT pe.playlist_id
              FROM play_events pe
              JOIN track_identities ti ON ti.id = pe.identity_id
             WHERE ti.broadcast_artist_id = %s
            """,
            (broadcast_artist_id,),
        ).fetchall()
        return [row["playlist_id"] for row in rows]

    def reject_open_songs(self, broadcast_artist_id: UUID) -> None:
        self._conn.execute(
            """
            UPDATE track_identities
            SET match_status = %s
            WHERE broadcast_artist_id = %s
              AND match_status != ALL(%s)
            """,
            (MatchStatus.AUTO_REJECTED.value, broadcast_artist_id, _PROTECTED_STATUSES),
        )

    def unmatch_if_decided(self, broadcast_artist_id: UUID) -> bool:
        # One UPDATE ... WHERE id AND match_status closes the SELECT-then-UPDATE window in
        # which a concurrent Resolve could change the row between a read and this write.
        row = self._conn.execute(
            """
            UPDATE broadcast_artists
               SET match_status  = %s,
                   reason_code   = %s,
                   reason_detail = NULL
             WHERE id = %s
               AND match_status = ANY(%s)
            RETURNING id
            """,
            (
                MatchStatus.NEEDS_REVIEW.value,
                ReasonCode.USER_UNMATCHED.value,
                broadcast_artist_id,
                _UNMATCHABLE_STATUSES,
            ),
        ).fetchone()
        return row is not None

    def reopen_every_song(self, broadcast_artist_id: UUID) -> None:
        self._conn.execute(
            """
            UPDATE track_identities
               SET match_status  = %s,
                   match_tier    = NULL,
                   reason_code   = %s,
                   reason_detail = NULL
             WHERE broadcast_artist_id = %s
            """,
            (MatchStatus.NEEDS_REVIEW.value, ReasonCode.USER_UNMATCHED.value, broadcast_artist_id),
        )

    def delete_song_matches(self, broadcast_artist_id: UUID) -> None:
        self._conn.execute(
            """
            DELETE FROM matches
             WHERE identity_id IN (
                 SELECT id FROM track_identities
                  WHERE broadcast_artist_id = %s
             )
            """,
            (broadcast_artist_id,),
        )
