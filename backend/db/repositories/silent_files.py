"""The Library Silent Files report's read (D111, S6).

A lost connection becomes ``StorageUnavailableError`` (503 at the route). Unlike the stream's
reads it has no read bounds (D88): a lock or statement timeout on a live connection passes
through as a 500, which is acceptable for a page opened by hand.
"""

from __future__ import annotations

from typing import Any

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.streaming import (
    NEARLY_SILENT_MAX_CUED_PERCENT,
    NEARLY_SILENT_MAX_KBPS,
    NEARLY_SILENT_MIN_DURATION_MS,
    SilentFileEntry,
)
from backend.domain.system import StorageUnavailableError
from backend.repositories.silent_files import SilentFileReportRepository

# The domain's rules (SilentFileEntry.reasons) as a pre-filter, in the same integer arithmetic
# (bits per millisecond is kb/s), so the two agree at every boundary; the domain names the
# reasons. Present files only, one row per file (twins of silent audio are each listed). A
# fallback (failed) row's span is no signal, so cued_span_ms is NULL for it. A backwards cue row
# (cue-out before cue-in) reads as a zero span rather than a negative one the report would reject.
_CANDIDATES_SQL = """
SELECT f.id, f.file_path, f.artist_name, f.track_title, f.release_title,
       f.duration_ms, f.file_size,
       COALESCE(c.audio_silent, false) AS audio_silent,
       CASE WHEN NOT c.analysis_failed THEN GREATEST(c.cue_out_ms - c.cue_in_ms, 0) END
           AS cued_span_ms
  FROM library_files f
  LEFT JOIN stream_cues c ON c.audio_hash = f.audio_hash
 WHERE f.file_status = 'present'
   AND (c.audio_silent
        OR (f.duration_ms > %(min_ms)s
            AND (f.file_size * 8 < %(max_kbps)s::bigint * f.duration_ms
                 OR (NOT c.analysis_failed
                     AND (c.cue_out_ms - c.cue_in_ms)::bigint * 100
                         < %(max_pct)s::bigint * f.duration_ms))))
 ORDER BY f.file_path
"""


def _entry(r: dict[str, Any]) -> SilentFileEntry:
    return SilentFileEntry(
        file_id=r["id"],
        path=r["file_path"],
        artist=r["artist_name"],
        title=r["track_title"],
        album=r["release_title"],
        duration_ms=r["duration_ms"],
        file_size=r["file_size"],
        audio_silent=bool(r["audio_silent"]),
        cued_span_ms=r["cued_span_ms"],
    )


@translate_lost_connection(StorageUnavailableError)
class PgSilentFileReportRepository(SilentFileReportRepository):
    """PostgreSQL implementation of :class:`SilentFileReportRepository`."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def candidates(self) -> tuple[SilentFileEntry, ...]:
        params = {
            "min_ms": NEARLY_SILENT_MIN_DURATION_MS,
            "max_kbps": NEARLY_SILENT_MAX_KBPS,
            "max_pct": NEARLY_SILENT_MAX_CUED_PERCENT,
        }
        rows = self._conn.execute(_CANDIDATES_SQL, params).fetchall()
        return tuple(_entry(r) for r in rows)
