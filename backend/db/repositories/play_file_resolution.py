from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from backend.domain.curation import PlayFileResolution
from backend.domain.enums import FileStatus
from backend.repositories.play_file_resolution import PlayFileResolutionRepository

# The view is read PER PLAY, as its COMMENT ON VIEW requires: OFFSET 0 fences the LATERAL
# subquery so the planner cannot pull it up and compute the view for every play (about 10 s
# on dev). The join is LEFT so that every requested id comes back, resolved or not.
# The final file's audio's cue row says whether it is silent (D111, S7), joined as the
# playable schedule's reader joins it; with no row, audio_silent reads false.
_FOR_PLAYS_SQL = """
SELECT ids.play_event_id, res.file_id, res.file_status,
       COALESCE(c.audio_silent, false) AS audio_silent
FROM unnest(%(ids)s::uuid[]) AS ids(play_event_id)
LEFT JOIN LATERAL (
    SELECT r.file_id, r.file_status
    FROM play_file_resolution r
    WHERE r.play_event_id = ids.play_event_id
    OFFSET 0
) res ON true
LEFT JOIN library_files f ON f.id = res.file_id
LEFT JOIN stream_cues c ON c.audio_hash = f.audio_hash
"""


class PgPlayFileResolutionRepository(PlayFileResolutionRepository):
    """PostgreSQL implementation of :class:`PlayFileResolutionRepository`."""

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def get_for_plays(self, play_event_ids: Sequence[UUID]) -> dict[UUID, PlayFileResolution]:
        if not play_event_ids:
            return {}
        rows = self._conn.execute(_FOR_PLAYS_SQL, {"ids": list(play_event_ids)}).fetchall()
        return {row["play_event_id"]: _to_resolution(row) for row in rows}


def _to_resolution(row: DictRow) -> PlayFileResolution:
    status = row["file_status"]
    return PlayFileResolution(
        play_event_id=row["play_event_id"],
        file_id=row["file_id"],
        file_status=None if status is None else FileStatus(status),
        audio_silent=bool(row["audio_silent"]),
    )
