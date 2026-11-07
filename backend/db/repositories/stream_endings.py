from __future__ import annotations

import psycopg
import structlog
from psycopg.rows import DictRow

from backend.domain.endings import (
    ENDING_ANALYSER_VERSION,
    EndingCandidate,
    EndingType,
    SongEnding,
)
from backend.domain.library import AudioHash
from backend.domain.streaming import CuePoints, InvalidStreamValueError
from backend.repositories.stream_endings import StreamEndingRepository

logger = structlog.get_logger()

_GET_SQL = """
SELECT ending, eom_ms, measured_cue_out_ms FROM stream_endings WHERE audio_hash = %s
"""

_NEEDING_SQL = """
SELECT c.audio_hash,
       c.cue_in_ms, c.cue_out_ms, c.fade_in_ms, c.fade_out_ms, c.start_next_ms, c.gain_db,
       (SELECT min(f.file_path) FROM library_files f
         WHERE f.audio_hash = c.audio_hash AND f.file_status = 'present') AS path
FROM stream_cues c
LEFT JOIN stream_endings n ON n.audio_hash = c.audio_hash
WHERE NOT c.analysis_failed
  AND NOT c.audio_silent
  AND (n.audio_hash IS NULL OR n.measured_cue_out_ms <> c.cue_out_ms)
  AND (%(after)s::text IS NULL OR c.audio_hash > %(after)s::text)
  AND EXISTS (SELECT 1 FROM library_files f
               WHERE f.audio_hash = c.audio_hash AND f.file_status = 'present')
ORDER BY c.audio_hash
LIMIT %(limit)s
"""

_STORE_SQL = """
INSERT INTO stream_endings (audio_hash, ending, eom_ms, measured_cue_out_ms, analyser_version,
                            analysed_at)
VALUES (%s, %s, %s, %s, %s, now())
ON CONFLICT (audio_hash) DO UPDATE SET
    ending              = EXCLUDED.ending,
    eom_ms              = EXCLUDED.eom_ms,
    measured_cue_out_ms = EXCLUDED.measured_cue_out_ms,
    analyser_version    = EXCLUDED.analyser_version,
    analysed_at         = EXCLUDED.analysed_at
"""

_PURGE_SQL = "DELETE FROM stream_endings WHERE analyser_version <> %s"

_PRUNE_SQL = """
DELETE FROM stream_endings n
WHERE NOT EXISTS (SELECT 1 FROM stream_cues c WHERE c.audio_hash = n.audio_hash)
"""


def song_ending(row: DictRow) -> SongEnding:
    """The ending a row with ``ending``, ``eom_ms`` and ``measured_cue_out_ms`` describes;
    raises ``ValueError`` (``InvalidStreamValueError`` included) on an invalid row."""
    return SongEnding(EndingType(row["ending"]), row["eom_ms"], row["measured_cue_out_ms"])


class PgStreamEndingRepository(StreamEndingRepository):
    """PostgreSQL implementation of :class:`StreamEndingRepository` (``stream_endings``)."""

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def get(self, audio_hash: AudioHash) -> SongEnding | None:
        row = self._conn.execute(_GET_SQL, (str(audio_hash),)).fetchone()
        if row is None:
            return None
        try:
            return song_ending(row)
        except ValueError as error:
            logger.warning("stream_ending_invalid", audio_hash=str(audio_hash), error=str(error))
            return None

    def needing_analysis(self, after: AudioHash | None, limit: int) -> list[EndingCandidate]:
        params = {"after": None if after is None else str(after), "limit": limit}
        candidates: list[EndingCandidate] = []
        for row in self._conn.execute(_NEEDING_SQL, params).fetchall():
            try:
                cues = CuePoints(
                    cue_in_ms=row["cue_in_ms"],
                    cue_out_ms=row["cue_out_ms"],
                    fade_in_ms=row["fade_in_ms"],
                    fade_out_ms=row["fade_out_ms"],
                    start_next_ms=row["start_next_ms"],
                    gain_db=row["gain_db"],
                )
            except InvalidStreamValueError as error:
                logger.warning(
                    "stream_ending_cues_invalid", audio_hash=row["audio_hash"], error=str(error)
                )
                continue
            candidates.append(
                EndingCandidate(AudioHash.parse(row["audio_hash"]), row["path"], cues)
            )
        return candidates

    def store(self, audio_hash: AudioHash, ending: SongEnding) -> None:
        params = (
            str(audio_hash),
            ending.ending.value,
            ending.eom_ms,
            ending.measured_cue_out_ms,
            ENDING_ANALYSER_VERSION,
        )
        self._conn.execute(_STORE_SQL, params)

    def purge_other_versions(self, current_version: int) -> None:
        self._conn.execute(_PURGE_SQL, (current_version,))

    def prune_without_cues(self) -> None:
        self._conn.execute(_PRUNE_SQL)
