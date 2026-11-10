"""PostgreSQL adapter of :class:`SongDecisionRepository`: the song Resolve, Reject and Unmatch
statements, moved from the router and ``identity_resolution_service`` unchanged (phase 3a PR 1),
on the request's sync connection."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.enums import MatchStatus, MatchTier, ReasonCode
from backend.domain.matching import MatchingStorageError
from backend.repositories.song_decisions import SongDecisionRepository


@translate_lost_connection(MatchingStorageError)
class PgSongDecisionRepository(SongDecisionRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def exists(self, identity_id: UUID) -> bool:
        row = self._conn.execute(
            "SELECT id FROM track_identities WHERE id = %s", (identity_id,)
        ).fetchone()
        return row is not None

    def lock_status(self, identity_id: UUID) -> str | None:
        row = self._conn.execute(
            "SELECT match_status FROM track_identities WHERE id = %s FOR UPDATE", (identity_id,)
        ).fetchone()
        return None if row is None else str(row["match_status"])

    def matched_file_ids(self, identity_id: UUID) -> list[UUID]:
        rows = self._conn.execute(
            """SELECT DISTINCT library_file_id FROM matches
                WHERE identity_id = %s AND library_file_id IS NOT NULL""",
            (identity_id,),
        ).fetchall()
        return [row["library_file_id"] for row in rows]

    def set_matched_by_hand(self, identity_id: UUID) -> None:
        self._conn.execute(
            "UPDATE track_identities SET match_status = %s, match_tier = %s WHERE id = %s",
            (MatchStatus.MANUAL_MATCHED.value, MatchTier.MANUAL.value, identity_id),
        )

    def park_rejected(self, identity_id: UUID, file_ids: Sequence[UUID]) -> None:
        self._conn.execute(
            """UPDATE track_identities
                  SET rejected_file_ids = COALESCE(
                          (SELECT array_agg(DISTINCT x)
                             FROM unnest(rejected_file_ids || %s::uuid[]) AS x),
                          '{}'),
                      match_status  = %s,
                      match_tier    = NULL,
                      reason_code   = %s,
                      reason_detail = NULL
                WHERE id = %s""",
            (
                list(file_ids),
                MatchStatus.MANUAL_REJECTED.value,
                ReasonCode.USER_UNMATCHED.value,
                identity_id,
            ),
        )
