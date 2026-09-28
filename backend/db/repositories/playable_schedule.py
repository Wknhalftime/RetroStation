from __future__ import annotations

import dataclasses
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

# One station-day in one statement (spec: Data, D17-D20). The reader holds no rules of its
# own; it adds times, titles and cues to what the two views say.
# - Plays, the day window and the order are broadcast's view station_day_plays (D19):
#   filter on station_id and play_date, order by position.
# - Which file plays is curation's view play_file_resolution (D17). It is joined PER PLAY:
#   OFFSET 0 fences the LATERAL subquery so the planner cannot pull it up and compute the
#   view for every play (on dev, 5M plays: 9.2 s unfenced, about 8 ms fenced).
# - Cues are the final file's audio's row, if any (D20): no stat or version check.
_DAY_SQL = """
SELECT dp.play_event_id AS event_id,
       dp.logged_at,
       ti.original_title AS title,
       ba.original_name AS artist,
       res.file_id, res.file_status, res.source,
       f.file_path, f.duration_ms,
       c.audio_hash AS cued_audio,
       c.cue_in_ms, c.cue_out_ms, c.fade_in_ms, c.fade_out_ms, c.start_next_ms, c.gain_db
FROM station_day_plays dp
JOIN track_identities ti ON ti.id = dp.identity_id
JOIN broadcast_artists ba ON ba.id = ti.broadcast_artist_id
JOIN LATERAL (
    SELECT r.file_id, r.file_status, r.source
    FROM play_file_resolution r
    WHERE r.play_event_id = dp.play_event_id
    OFFSET 0
) res ON true
LEFT JOIN library_files f ON f.id = res.file_id
LEFT JOIN stream_cues c ON c.audio_hash = f.audio_hash
WHERE dp.station_id = %(station_id)s
  AND dp.play_date = %(day)s
ORDER BY dp.position
"""


class PgPlayableScheduleRepository(PlayableScheduleRepository):
    """PostgreSQL implementation of :class:`PlayableScheduleRepository`."""

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def get_day(self, station_id: UUID, day: date) -> list[ScheduleItem]:
        rows = self._conn.execute(_DAY_SQL, {"station_id": station_id, "day": day}).fetchall()
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
    """The resolved file, or none: unresolved, not present (D21) or invalid (D18).

    One warning per play: the checks run availability, then the file, then its cues, and
    the first that fails ends them, so a bad file's cues are never read.
    """
    if row["file_id"] is None:
        return None
    if row["file_status"] != FileStatus.PRESENT:
        logger.warning(
            "schedule_file_unavailable",
            event_id=str(row["event_id"]),
            file_id=str(row["file_id"]),
            file_status=row["file_status"],
            source=row["source"],
        )
        return None
    file = _to_valid_file(row)
    if file is None:
        return None
    return dataclasses.replace(file, cues=_to_cues(row))


def _to_valid_file(row: DictRow) -> PlayableFile | None:
    """The file row without cues, or none if it fails validation (logged, never raised)."""
    try:
        return PlayableFile(
            file_id=row["file_id"],
            path=row["file_path"],
            duration_ms=row["duration_ms"],
            cues=None,
        )
    except InvalidStreamValueError as error:
        logger.warning(
            "schedule_file_invalid",
            event_id=str(row["event_id"]),
            file_id=str(row["file_id"]),
            error=str(error),
        )
        return None


def _to_cues(row: DictRow) -> CuePoints | None:
    """The audio's cue points, or none. A row that fails validation is logged, not raised."""
    if row["cued_audio"] is None:
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
