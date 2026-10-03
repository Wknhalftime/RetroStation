from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.rows import DictRow

from backend.db.repositories.stream_cue_work import NEEDS_ANALYSIS, PRESENT_WITH_HASH
from backend.db.stream_reads import ReadBounds, bounded_connection
from backend.domain.streaming import CueCoverage, StreamReadError
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
        if row is None:
            # A single-row aggregate query always answers (T6.5); seeing none means the
            # database did not, in fact, answer at all (D88).
            raise StreamReadError("the cue coverage query returned no row")
        ready = row["ready"]
        failed = row["failed"]
        return CueCoverage(
            analysable=row["waiting"] + ready + failed,
            ready=ready,
            failed=failed,
            unhashed=row["unhashed"],
        )


@dataclass(frozen=True)
class BoundedCueCoverageRepository(CueCoverageRepository):
    """Cue coverage read on a fresh, bounded connection opened only when ``coverage()`` runs
    (PG7, I7), never when this object is built.

    A FastAPI dependency that opened the connection eagerly (e.g. a generator dependency that
    opens it before yielding) would raise a failed connection during dependency resolution,
    before the route body runs; the route's own error handling can never catch it there (the
    coordinator's review on PR G2's Task 6). Opening it lazily, inside ``coverage()``, means
    the one call the route makes is the one that can fail, so the route's try/except maps the
    resulting ``StreamReadError`` to 503 like every other stream read (D88).
    """

    database_url: str
    bounds: ReadBounds

    def coverage(self) -> CueCoverage:
        with bounded_connection(self.database_url, self.bounds, autocommit=True) as conn:
            return PgCueCoverageRepository(conn).coverage()
