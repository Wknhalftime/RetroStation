from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import psycopg
from psycopg.rows import DictRow

from backend.domain.library import AudioHash
from backend.domain.streaming import CueCandidate
from backend.repositories.stream_cue_work import CueWorkRepository

_CANDIDATE = "f.id, f.file_path, f.audio_hash, f.duration_ms, f.file_size, f.file_mtime_ns"

# Needs analysis (D20): a present file (D21) with an audio_hash whose audio has no cue row.
_NEEDS_ANALYSIS = """f.file_status = 'present'
  AND f.audio_hash IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM stream_cues c WHERE c.audio_hash = f.audio_hash)"""

# file_path is unique, so the keyset is total (as in the hash backfill).
_LIBRARY_SQL = f"""
SELECT {_CANDIDATE}
FROM library_files f
WHERE {_NEEDS_ANALYSIS}
  AND (%(after)s::text IS NULL OR f.file_path > %(after)s)
ORDER BY f.file_path
LIMIT %(limit)s
"""

# The plays on the days come from broadcast's station_day_plays (D19), which files they
# play from curation's play_file_resolution (D17, D22), joined PER PLAY: OFFSET 0 fences the
# lateral so the view is never computed for every play (the fence test pins it). No
# resolution rules of its own.
_SCHEDULED_SQL = f"""
SELECT DISTINCT {_CANDIDATE}
FROM station_day_plays dp
CROSS JOIN LATERAL (
    SELECT r.file_id
    FROM play_file_resolution r
    WHERE r.play_event_id = dp.play_event_id
    OFFSET 0
) res
JOIN library_files f ON f.id = res.file_id
WHERE dp.station_id IN (SELECT id FROM stations)
  AND dp.play_date = ANY(%(days)s)
  AND {_NEEDS_ANALYSIS}
ORDER BY f.file_path
"""

# The years are not "a station's plays on a date" (D19), so this reads play_events directly,
# with D3's UTC-label date expression, which idx_play_events_play_date serves.
_YEARS_SQL = """
SELECT extract(year FROM min((played_at AT TIME ZONE 'UTC')::date))::int AS first,
       extract(year FROM max((played_at AT TIME ZONE 'UTC')::date))::int AS last
FROM play_events
"""


def _candidate(row: DictRow) -> CueCandidate:
    return CueCandidate(
        file_id=row["id"],
        path=row["file_path"],
        audio_hash=AudioHash.parse(row["audio_hash"]),
        duration_ms=row["duration_ms"],
        file_size=row["file_size"],
        file_mtime_ns=row["file_mtime_ns"],
    )


class PgCueWorkRepository(CueWorkRepository):
    """PostgreSQL implementation of :class:`CueWorkRepository`."""

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def logged_years(self) -> range:
        row = self._conn.execute(_YEARS_SQL).fetchone()
        if row is None or row["first"] is None:
            return range(0)
        return range(row["first"], row["last"] + 1)

    def scheduled(self, days: Sequence[date]) -> list[CueCandidate]:
        rows = self._conn.execute(_SCHEDULED_SQL, {"days": list(days)}).fetchall()
        return [_candidate(row) for row in rows]

    def library(self, after_path: str | None, limit: int) -> list[CueCandidate]:
        rows = self._conn.execute(_LIBRARY_SQL, {"after": after_path, "limit": limit}).fetchall()
        return [_candidate(row) for row in rows]
