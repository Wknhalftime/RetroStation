from __future__ import annotations

import psycopg
from psycopg.rows import DictRow

from backend.domain.library import AudioHash
from backend.domain.streaming import CueMarkers
from backend.repositories.stream_cue_edits import StreamCueEditRepository

_GET_SQL = """
SELECT start_ms, eom_ms, fade_at_ms, stop_ms FROM stream_cue_edits WHERE audio_hash = %s
"""

_SAVE_SQL = """
INSERT INTO stream_cue_edits (audio_hash, start_ms, eom_ms, fade_at_ms, stop_ms, edited_at)
VALUES (%s, %s, %s, %s, %s, now())
ON CONFLICT (audio_hash) DO UPDATE SET
    start_ms   = EXCLUDED.start_ms,
    eom_ms     = EXCLUDED.eom_ms,
    fade_at_ms = EXCLUDED.fade_at_ms,
    stop_ms    = EXCLUDED.stop_ms,
    edited_at  = EXCLUDED.edited_at
"""

_DELETE_SQL = "DELETE FROM stream_cue_edits WHERE audio_hash = %s"


class PgStreamCueEditRepository(StreamCueEditRepository):
    """PostgreSQL implementation of :class:`StreamCueEditRepository` (``stream_cue_edits``).

    Keyed by ``audio_hash`` alone, with no foreign key (D20): an edit needs no library file.
    The table's CHECK constraints refuse a row that breaks the marker ordering.
    """

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def get(self, audio_hash: AudioHash) -> CueMarkers | None:
        row = self._conn.execute(_GET_SQL, (str(audio_hash),)).fetchone()
        if row is None:
            return None
        return CueMarkers(
            start_ms=row["start_ms"],
            eom_ms=row["eom_ms"],
            fade_at_ms=row["fade_at_ms"],
            stop_ms=row["stop_ms"],
        )

    def save(self, audio_hash: AudioHash, markers: CueMarkers) -> None:
        params = (
            str(audio_hash),
            markers.start_ms,
            markers.eom_ms,
            markers.fade_at_ms,
            markers.stop_ms,
        )
        self._conn.execute(_SAVE_SQL, params)

    def delete(self, audio_hash: AudioHash) -> None:
        self._conn.execute(_DELETE_SQL, (str(audio_hash),))
