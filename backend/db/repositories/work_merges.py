"""PostgreSQL adapter for the guarded merge's port (phase 3b PR 3; AUD-R079)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from uuid import UUID

import psycopg
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from backend.domain.work_merges import (
    LineKey,
    LineResult,
    WorkMergeGroup,
    WorkMergeStorageError,
)
from backend.repositories.work_merges import MasterLine, WorkMergeRepository

_BEST = """LATERAL (
    SELECT m.id, m.library_file_id
      FROM matches m
     WHERE m.identity_id = ti.id
       AND m.library_file_id IS NOT NULL
       AND ti.match_status IN ('auto_matched', 'manual_matched')
     ORDER BY m.confidence_score DESC, m.created_at, m.id
     LIMIT 1
)"""

_LINES_OF_WORKS = f"""
WITH candidates AS (
    SELECT DISTINCT m.identity_id
      FROM matches m
      JOIN library_files f ON f.id = m.library_file_id
      LEFT JOIN recordings r ON r.id = f.recording_id
     WHERE f.work_id = ANY(%(works)s) OR r.work_id = ANY(%(works)s)
)
SELECT ti.id
  FROM candidates c
  JOIN track_identities ti ON ti.id = c.identity_id
 CROSS JOIN {_BEST} best
  JOIN library_files f ON f.id = best.library_file_id
  LEFT JOIN recordings r ON r.id = f.recording_id
 WHERE COALESCE(f.work_id, r.work_id) = ANY(%(works)s)
"""

# One play per (station, line) stands for them all: the view resolves a line the same way at
# every play of one station.
_RESULTS = """
SELECT sip.station_id, st.call_letters AS station, ti.id AS identity_id,
       ti.original_title AS title, ba.original_name AS artist, sip.play_count AS plays,
       v.file_id, v.source, f.file_path AS path
  FROM station_identity_plays sip
  JOIN stations st ON st.id = sip.station_id
  JOIN track_identities ti ON ti.id = sip.identity_id
  JOIN broadcast_artists ba ON ba.id = ti.broadcast_artist_id
 CROSS JOIN LATERAL (
     SELECT pe.id FROM play_events pe JOIN playlists pl ON pl.id = pe.playlist_id
      WHERE pe.identity_id = ti.id AND pl.station_id = sip.station_id
      LIMIT 1
 ) one
 CROSS JOIN LATERAL (
     SELECT r.file_id, r.source FROM play_file_resolution r
      WHERE r.play_event_id = one.id OFFSET 0
 ) v
  LEFT JOIN library_files f ON f.id = v.file_id
 WHERE sip.identity_id = ANY(%s) AND sip.play_count > 0
"""

_MASTER_LINES = f"""
SELECT DISTINCT best.id AS match_id, ti.id AS identity_id, v.file_id,
       ARRAY(SELECT l.artist_id FROM log_name_links l
              WHERE l.broadcast_artist_id = ti.broadcast_artist_id AND l.state = 'active')
           AS artists
  FROM station_identity_plays sip
  JOIN track_identities ti ON ti.id = sip.identity_id
 CROSS JOIN {_BEST} best
 CROSS JOIN LATERAL (
     SELECT pe.id FROM play_events pe JOIN playlists pl ON pl.id = pe.playlist_id
      WHERE pe.identity_id = ti.id AND pl.station_id = sip.station_id
      LIMIT 1
 ) one
 CROSS JOIN LATERAL (
     SELECT r.file_id, r.source FROM play_file_resolution r
      WHERE r.play_event_id = one.id OFFSET 0
 ) v
 WHERE sip.identity_id = ANY(%s) AND sip.play_count > 0 AND v.source = 'master'
"""


def _uuid(value: object) -> UUID | None:
    return None if value is None else UUID(str(value))


def _new_id(row: DictRow | None) -> UUID:
    if row is None:
        raise WorkMergeStorageError("work_merges returned no id")
    return UUID(str(row["id"]))


class PgWorkMergeRepository(WorkMergeRepository):
    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def lines_of_works(self, work_ids: Sequence[str]) -> list[UUID]:
        rows = self._conn.execute(_LINES_OF_WORKS, {"works": list(work_ids)}).fetchall()
        return [UUID(str(r["id"])) for r in rows]

    def line_results(self, identity_ids: Sequence[UUID]) -> dict[LineKey, LineResult]:
        rows = self._conn.execute(_RESULTS, (list(identity_ids),)).fetchall()
        found: dict[LineKey, LineResult] = {}
        for r in rows:
            result = LineResult(
                station_id=UUID(str(r["station_id"])),
                station=str(r["station"]),
                identity_id=UUID(str(r["identity_id"])),
                title=str(r["title"]),
                artist=str(r["artist"]),
                plays=int(r["plays"]),
                file_id=_uuid(r["file_id"]),
                path=r["path"],
            )
            found[(result.station_id, result.identity_id)] = result
        return found

    def master_lines(self, work_ids: Sequence[str]) -> list[MasterLine]:
        lines = self.lines_of_works(work_ids)
        if not lines:
            return []
        rows = self._conn.execute(_MASTER_LINES, (lines,)).fetchall()
        return [
            MasterLine(
                match_id=UUID(str(r["match_id"])),
                identity_id=UUID(str(r["identity_id"])),
                master_file_id=UUID(str(r["file_id"])),
                artists=frozenset(str(a) for a in r["artists"]),
            )
            for r in rows
            if r["file_id"] is not None
        ]

    def in_savepoint[T](self, attempt: Callable[[], tuple[T, bool]]) -> T:
        # psycopg's Rollback undoes the savepoint and is swallowed by its own block.
        try:
            with self._conn.transaction() as savepoint:
                value, keep = attempt()
                if not keep:
                    raise psycopg.Rollback(savepoint)
        except psycopg.Error as error:
            raise WorkMergeStorageError(str(error)) from error
        return value

    def start(self, group: WorkMergeGroup) -> UUID:
        row = self._conn.execute(
            """INSERT INTO work_merges (target_id, source_ids, reason, approvals, status)
               VALUES (%s, %s, %s, %s, 'merged') RETURNING id""",
            (
                group.target_id,
                list(group.source_ids),
                group.reason.value,
                Jsonb(
                    [
                        {
                            "station_id": str(a.station_id),
                            "identity_id": str(a.identity_id),
                            "file_id": None if a.file_id is None else str(a.file_id),
                        }
                        for a in sorted(group.approvals, key=str)
                    ]
                ),
            ),
        ).fetchone()
        merge_id = _new_id(row)
        self._conn.execute("SELECT set_config('retrostation.merge_id', %s, true)", (str(merge_id),))
        return merge_id

    def stop(self) -> None:
        self._conn.execute("SELECT set_config('retrostation.merge_id', '', true)")

    def redirect(self, old_ids: Sequence[str], new_id: str, merge_id: UUID) -> None:
        ids = list(old_ids)
        self._conn.execute(
            "UPDATE work_redirects SET new_id = %s WHERE new_id = ANY(%s)", (new_id, ids)
        )
        self._conn.execute(
            """INSERT INTO work_redirects (old_id, new_id, merge_id)
               SELECT unnest(%s::text[]), %s, %s
               ON CONFLICT (old_id) DO UPDATE SET new_id = EXCLUDED.new_id""",
            (ids, new_id, merge_id),
        )

    def log_failure(self, group: WorkMergeGroup, error: str) -> UUID:
        row = self._conn.execute(
            """INSERT INTO work_merges (target_id, source_ids, reason, status, error)
               VALUES (%s, %s, %s, 'failed', %s) RETURNING id""",
            (group.target_id, list(group.source_ids), group.reason.value, error),
        ).fetchone()
        return _new_id(row)
