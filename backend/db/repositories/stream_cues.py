from __future__ import annotations

import psycopg
from psycopg.rows import DictRow

from backend.domain.streaming import CueAnalysis
from backend.repositories.stream_cues import StreamCueRepository

_UPSERT_SQL = """
INSERT INTO stream_cues
    (audio_hash, cue_in_ms, cue_out_ms, fade_in_ms, fade_out_ms,
     start_next_ms, loudness_lufs, gain_db, analyser_version, analysis_failed, analysed_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
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
    analysed_at      = EXCLUDED.analysed_at
"""


class PgStreamCueRepository(StreamCueRepository):
    """PostgreSQL implementation of :class:`StreamCueRepository` (table ``stream_cues``)."""

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def upsert(self, analysis: CueAnalysis) -> None:
        """Store ``analysis`` as its audio's cues; no library file needs to exist (D20).

        Keyed by ``audio_hash`` alone: there is no foreign key to violate, so no savepoint
        and no FK translation are needed here.
        """
        cues = analysis.cues
        params = (
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
        self._conn.execute(_UPSERT_SQL, params)
