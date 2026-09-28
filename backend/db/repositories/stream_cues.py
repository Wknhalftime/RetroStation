from __future__ import annotations

import psycopg
from psycopg.rows import DictRow

from backend.domain.streaming import CueAnalysis, CueFileNotFoundError
from backend.repositories.stream_cues import StreamCueRepository

_UPSERT_SQL = """
INSERT INTO stream_cues
    (library_file_id, cue_in_ms, cue_out_ms, fade_in_ms, fade_out_ms,
     start_next_ms, loudness_lufs, gain_db, file_size, file_mtime_ns,
     analyser_version, analysis_failed, analysed_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (library_file_id) DO UPDATE SET
    cue_in_ms        = EXCLUDED.cue_in_ms,
    cue_out_ms       = EXCLUDED.cue_out_ms,
    fade_in_ms       = EXCLUDED.fade_in_ms,
    fade_out_ms      = EXCLUDED.fade_out_ms,
    start_next_ms    = EXCLUDED.start_next_ms,
    loudness_lufs    = EXCLUDED.loudness_lufs,
    gain_db          = EXCLUDED.gain_db,
    file_size        = EXCLUDED.file_size,
    file_mtime_ns    = EXCLUDED.file_mtime_ns,
    analyser_version = EXCLUDED.analyser_version,
    analysis_failed  = EXCLUDED.analysis_failed,
    analysed_at      = EXCLUDED.analysed_at
"""


class PgStreamCueRepository(StreamCueRepository):
    """PostgreSQL implementation of :class:`StreamCueRepository` (table ``stream_cues``)."""

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def upsert(self, analysis: CueAnalysis) -> None:
        """Store ``analysis``; a file deleted meanwhile raises :class:`CueFileNotFoundError`.

        The write runs in a savepoint, so that failure leaves the caller's transaction usable.
        """
        cues = analysis.cues
        params = (
            analysis.file_id,
            cues.cue_in_ms,
            cues.cue_out_ms,
            cues.fade_in_ms,
            cues.fade_out_ms,
            cues.start_next_ms,
            analysis.loudness_lufs,
            cues.gain_db,
            analysis.file_size,
            analysis.file_mtime_ns,
            analysis.analyser_version,
            analysis.analysis_failed,
        )
        try:
            with self._conn.transaction():
                self._conn.execute(_UPSERT_SQL, params)
        except psycopg.errors.ForeignKeyViolation as error:
            raise CueFileNotFoundError(analysis.file_id) from error
