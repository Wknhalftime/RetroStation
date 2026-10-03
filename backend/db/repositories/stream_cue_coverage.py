from __future__ import annotations

import psycopg
from psycopg.rows import DictRow

from backend.db.repositories.stream_cue_work import NEEDS_ANALYSIS, PRESENT_WITH_HASH
from backend.domain.streaming import CueCoverage
from backend.repositories.stream_cue_coverage import CueCoverageRepository

# D20, H8: "waiting" is read through the one "needs analysis" fragment (NEEDS_ANALYSIS), not
# a second definition; "ready"/"failed" share its baseline (PRESENT_WITH_HASH) and add the
# settled-row check. Counts are per audio (DISTINCT audio_hash): twins share one cue row
# (D20), so they are one audio. ``unhashed`` is per file (D20: a file with no hash never gets
# cues, so there is no audio to dedupe by).
_COVERAGE_SQL = f"""
SELECT
    (SELECT count(DISTINCT f.audio_hash) FROM library_files f
       WHERE {NEEDS_ANALYSIS}) AS waiting,
    (SELECT count(DISTINCT f.audio_hash) FROM library_files f
       JOIN stream_cues c ON c.audio_hash = f.audio_hash
       WHERE {PRESENT_WITH_HASH} AND NOT c.analysis_failed) AS ready,
    (SELECT count(DISTINCT f.audio_hash) FROM library_files f
       JOIN stream_cues c ON c.audio_hash = f.audio_hash
       WHERE {PRESENT_WITH_HASH} AND c.analysis_failed) AS failed,
    (SELECT count(*) FROM library_files f
       WHERE f.file_status = 'present' AND f.audio_hash IS NULL) AS unhashed
"""


class PgCueCoverageRepository(CueCoverageRepository):
    """PostgreSQL implementation of :class:`CueCoverageRepository` (D77, D89; PG7)."""

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def coverage(self) -> CueCoverage:
        row = self._conn.execute(_COVERAGE_SQL).fetchone()
        assert row is not None  # a single-row aggregate query always answers (T6.5)
        ready = row["ready"]
        failed = row["failed"]
        return CueCoverage(
            analysable=row["waiting"] + ready + failed,
            ready=ready,
            failed=failed,
            unhashed=row["unhashed"],
        )
