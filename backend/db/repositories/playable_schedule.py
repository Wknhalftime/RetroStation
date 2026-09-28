from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg
import structlog
from psycopg.rows import DictRow

from backend.domain.enums import FileStatus
from backend.domain.streaming import (
    CuePoints,
    InvalidStreamValueError,
    PlayableFile,
    ScheduleItem,
)
from backend.repositories.playable_schedule import PlayableScheduleRepository

logger = structlog.get_logger()

_UNAVAILABLE = frozenset({FileStatus.MISSING, FileStatus.DELETED})

# One station-day in one query (spec: Data, D3, D4, D16).
# - Window: a sargable range on played_at, bounded by the day's midnights under the UTC
#   label. `timestamp AT TIME ZONE 'UTC'` is an absolute instant, so the bounds, and
#   `played_at AT TIME ZONE 'UTC'` for logged_at, do not depend on the session TimeZone.
# - best_match: per matched identity, the highest confidence, then the earliest, then the
#   lowest id. Match rows without a file cannot resolve, so they are not candidates.
# - resolved: the station format's override > song master > direct file. The work comes
#   from the direct file's recording; with no recording or no work, the direct file.
# - Cues join only when fresh: the current analyser version and the file's current stat.
# - Order: (played_at, identity_id) per D4, then event id so that the order is total.
_DAY_SQL = """
WITH day_plays AS (
    SELECT pe.id AS event_id, pe.identity_id, pe.played_at
    FROM play_events pe
    JOIN playlists pl ON pl.id = pe.playlist_id
    WHERE pl.station_id = %(station_id)s
      AND pe.played_at >= (%(day)s::date::timestamp AT TIME ZONE 'UTC')
      AND pe.played_at <  ((%(day)s::date + 1)::timestamp AT TIME ZONE 'UTC')
),
best_match AS (
    SELECT DISTINCT ON (m.identity_id) m.identity_id, m.library_file_id
    FROM matches m
    JOIN track_identities ti ON ti.id = m.identity_id
    WHERE m.identity_id IN (SELECT identity_id FROM day_plays)
      AND m.library_file_id IS NOT NULL
      AND ti.match_status IN ('auto_matched', 'manual_matched')
    ORDER BY m.identity_id, m.confidence_score DESC, m.created_at, m.id
),
resolved AS (
    SELECT bm.identity_id,
           COALESCE(fo.preferred_file_id, sm.preferred_file_id, bm.library_file_id)
               AS file_id,
           CASE WHEN fo.preferred_file_id IS NOT NULL THEN 'override'
                WHEN sm.preferred_file_id IS NOT NULL THEN 'master'
                ELSE 'direct'
           END AS source
    FROM best_match bm
    JOIN library_files direct ON direct.id = bm.library_file_id
    LEFT JOIN recordings r ON r.id = direct.recording_id
    LEFT JOIN song_masters sm ON sm.work_id = r.work_id
    LEFT JOIN format_overrides fo
           ON fo.work_id = r.work_id
          AND fo.format_name = (SELECT format_name FROM stations WHERE id = %(station_id)s)
)
SELECT dp.event_id,
       dp.played_at AT TIME ZONE 'UTC' AS logged_at,
       ti.original_title AS title,
       ba.original_name AS artist,
       res.source,
       f.id AS file_id, f.file_path, f.duration_ms, f.file_status,
       c.library_file_id AS cued_file_id,
       c.cue_in_ms, c.cue_out_ms, c.fade_in_ms, c.fade_out_ms, c.start_next_ms, c.gain_db
FROM day_plays dp
JOIN track_identities ti ON ti.id = dp.identity_id
JOIN broadcast_artists ba ON ba.id = ti.broadcast_artist_id
LEFT JOIN resolved res ON res.identity_id = dp.identity_id
LEFT JOIN library_files f ON f.id = res.file_id
LEFT JOIN stream_cues c
       ON c.library_file_id = f.id
      AND c.analyser_version = %(analyser_version)s
      AND c.file_size IS NOT DISTINCT FROM f.file_size
      AND c.file_mtime_ns IS NOT DISTINCT FROM f.file_mtime_ns
ORDER BY dp.played_at, dp.identity_id, dp.event_id
"""


class PgPlayableScheduleRepository(PlayableScheduleRepository):
    """PostgreSQL implementation of :class:`PlayableScheduleRepository`.

    ``analyser_version`` is the cue analysis in force: stored cues of any other
    version are stale and read as no cues.
    """

    def __init__(self, conn: psycopg.Connection[DictRow], analyser_version: int) -> None:
        if analyser_version < 1:
            raise InvalidStreamValueError(
                "PgPlayableScheduleRepository.analyser_version must be >= 1, "
                f"got {analyser_version}"
            )
        self._conn = conn
        self._analyser_version = analyser_version

    def get_day(self, station_id: UUID, day: date) -> list[ScheduleItem]:
        rows = self._conn.execute(
            _DAY_SQL,
            {"station_id": station_id, "day": day, "analyser_version": self._analyser_version},
        ).fetchall()
        return [_to_item(row) for row in rows]


def _to_item(row: DictRow) -> ScheduleItem:
    return ScheduleItem(
        event_id=row["event_id"],
        logged_at=row["logged_at"],
        title=row["title"],
        artist=row["artist"],
        file=_to_file(row),
    )


def _to_file(row: DictRow) -> PlayableFile | None:
    """The resolved file; none when unresolved, or missing or deleted (no fallback, D16)."""
    if row["file_id"] is None:
        return None
    if row["file_status"] in _UNAVAILABLE:
        logger.warning(
            "schedule_file_unavailable",
            event_id=str(row["event_id"]),
            file_id=str(row["file_id"]),
            file_status=row["file_status"],
            source=row["source"],
        )
        return None
    return PlayableFile(
        file_id=row["file_id"],
        path=row["file_path"],
        duration_ms=row["duration_ms"],
        cues=_to_cues(row),
    )


def _to_cues(row: DictRow) -> CuePoints | None:
    """Fresh cue points, or none. A stored row that fails validation is logged, not raised."""
    if row["cued_file_id"] is None:
        return None
    try:
        return CuePoints(
            cue_in_ms=row["cue_in_ms"],
            cue_out_ms=row["cue_out_ms"],
            fade_in_ms=row["fade_in_ms"],
            fade_out_ms=row["fade_out_ms"],
            start_next_ms=row["start_next_ms"],
            gain_db=row["gain_db"],
        )
    except InvalidStreamValueError as error:
        logger.warning(
            "schedule_cues_invalid",
            event_id=str(row["event_id"]),
            file_id=str(row["file_id"]),
            error=str(error),
        )
        return None
