from __future__ import annotations

from typing import Any

import psycopg

from backend.domain.library import MissingFileListing, MissingFileRow
from backend.repositories.missing_files import MissingFileListingRepository

# Statuses are inlined so the planner can use idx_library_files_missing (0031).
_PAGE_SQL = """
    SELECT lf.id, lf.file_path, lf.artist_name, lf.track_title, lf.release_title,
           lf.missing_since, lf.work_id, w.title AS work_title,
           (SELECT count(*) FROM matches m
             WHERE m.library_file_id = lf.id AND m.identity_id IS NOT NULL) AS match_count,
           EXISTS (SELECT 1 FROM library_files p
                    WHERE p.work_id = lf.work_id AND p.file_status = 'present')
             AS work_has_present_file
      FROM library_files lf
      LEFT JOIN works w ON w.id = lf.work_id
     WHERE lf.file_status = 'missing'
     ORDER BY lf.file_path
     LIMIT %s OFFSET %s
"""

_TOTALS_SQL = """
    SELECT (SELECT count(*) FROM library_files WHERE file_status = 'missing') AS total,
           (SELECT count(*) FROM matches m
              JOIN library_files f ON f.id = m.library_file_id
             WHERE f.file_status = 'missing' AND m.identity_id IS NOT NULL)
             AS total_match_count
"""


def _row(r: dict[str, Any]) -> MissingFileRow:
    return MissingFileRow(
        id=r["id"],
        file_path=r["file_path"],
        artist_name=r["artist_name"],
        track_title=r["track_title"],
        release_title=r["release_title"],
        missing_since=r["missing_since"],
        work_id=r["work_id"],
        work_title=r["work_title"],
        match_count=int(r["match_count"]),
        work_has_present_file=bool(r["work_has_present_file"]),
    )


class PgMissingFileListingRepository(MissingFileListingRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def list_page(self, offset: int, limit: int) -> MissingFileListing:
        rows = self._conn.execute(_PAGE_SQL, (limit, offset)).fetchall()
        totals = self._conn.execute(_TOTALS_SQL).fetchone()
        return MissingFileListing(
            rows=tuple(_row(r) for r in rows),
            total=int(totals["total"]) if totals else 0,
            total_match_count=int(totals["total_match_count"]) if totals else 0,
        )
