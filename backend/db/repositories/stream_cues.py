from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from backend.domain.streaming import CueAnalysis
from backend.repositories.stream_cues import StreamCueRepository

_COLUMNS = """(audio_hash, cue_in_ms, cue_out_ms, fade_in_ms, fade_out_ms,
     start_next_ms, loudness_lufs, gain_db, analyser_version, analysis_failed, analysed_at)"""

# Every upsert clears the orphan mark (D66): audio analysed again is in the library.
_ON_CONFLICT = """
ON CONFLICT (audio_hash) DO UPDATE SET
    cue_in_ms        = EXCLUDED.cue_in_ms,
    cue_out_ms       = EXCLUDED.cue_out_ms,
    fade_in_ms       = EXCLUDED.fade_in_ms,
    fade_out_ms      = EXCLUDED.fade_out_ms,
    start_next_ms    = EXCLUDED.start_next_ms,
    loudness_lufs    = EXCLUDED.loudness_lufs,
    gain_db          = EXCLUDED.gain_db,
    analyser_version = EXCLUDED.analyser_version,
    analysis_failed  = EXCLUDED.analysis_failed,
    analysed_at      = EXCLUDED.analysed_at,
    orphaned_at      = NULL
"""

_UPSERT_SQL = f"""
INSERT INTO stream_cues {_COLUMNS}
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
{_ON_CONFLICT}"""

# Stored only while the file still carries the hash read before analysing (For PR D and PR E).
_STORE_IF_CURRENT_SQL = f"""
INSERT INTO stream_cues {_COLUMNS}
SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()
WHERE EXISTS (SELECT 1 FROM library_files WHERE id = %s AND audio_hash = %s)
{_ON_CONFLICT}"""

_PURGE_SQL = "DELETE FROM stream_cues WHERE analyser_version <> %s"

# D56, in order. "In the library" is any library file carrying the audio, whatever its status
# (a missing file may come back or be remapped); idx_library_files_audio_hash serves it.
_CLEAR_RETURNED_SQL = """
UPDATE stream_cues c SET orphaned_at = NULL
WHERE c.orphaned_at IS NOT NULL
  AND EXISTS (SELECT 1 FROM library_files f WHERE f.audio_hash = c.audio_hash)
"""
_DELETE_EXPIRED_SQL = "DELETE FROM stream_cues WHERE orphaned_at <= %(now)s - %(grace)s"
_MARK_ORPHANS_SQL = """
UPDATE stream_cues c SET orphaned_at = %(now)s
WHERE c.orphaned_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM library_files f WHERE f.audio_hash = c.audio_hash)
"""


def _values(analysis: CueAnalysis) -> tuple[object, ...]:
    cues = analysis.cues
    return (
        str(analysis.audio_hash),
        cues.cue_in_ms,
        cues.cue_out_ms,
        cues.fade_in_ms,
        cues.fade_out_ms,
        cues.start_next_ms,
        analysis.loudness_lufs,
        cues.gain_db,
        analysis.analyser_version,
        analysis.analysis_failed,
    )


class PgStreamCueRepository(StreamCueRepository):
    """PostgreSQL implementation of :class:`StreamCueRepository` (table ``stream_cues``)."""

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def upsert(self, analysis: CueAnalysis) -> None:
        """Store ``analysis`` as its audio's cues; no library file needs to exist (D20).

        Keyed by ``audio_hash`` alone: there is no foreign key to violate, so no savepoint
        and no FK translation are needed here.
        """
        self._conn.execute(_UPSERT_SQL, _values(analysis))

    def store_if_current(self, analysis: CueAnalysis, file_id: UUID) -> bool:
        params = (*_values(analysis), file_id, str(analysis.audio_hash))
        return self._conn.execute(_STORE_IF_CURRENT_SQL, params).rowcount == 1

    def purge_other_versions(self, current_version: int) -> None:
        self._conn.execute(_PURGE_SQL, (current_version,))

    def prune_orphans(self, now: datetime, grace: timedelta) -> None:
        params = {"now": now, "grace": grace}
        self._conn.execute(_CLEAR_RETURNED_SQL)
        self._conn.execute(_DELETE_EXPIRED_SQL, params)
        self._conn.execute(_MARK_ORPHANS_SQL, params)
