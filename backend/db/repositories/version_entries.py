"""PostgreSQL version pages: the (work, version text) rows of matched lines and version entries,
with their plays and states (versions phase 2b PR 3, SPEC S5, S7, S8; design C60, C62).

A matched line is the air-play view's: auto or manual matched, its best match with a file
(highest confidence, then created_at, id), that file's own work, else its recording's. The SQL is
shared by the sync repository (the writes, Dismiss all) and the async reader (the read routes).

Plays, stations and the first play count only plays in a station's playlists: the counts come
from ``station_identity_plays`` (migration 0040, kept by triggers), which counts a play under its
playlist's station, so a play in a playlist with no station is not counted. The dev database had
none on 2026-11-09 (Lance's "Rewrite + rerun": reading 7.4M play_events made the Unmapped
versions page take 1.4 s). Each query comes in two variants, every work and a scoped one, so a
prepared generic plan never keeps an "every work OR" branch and loses the work filter.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

import psycopg
from psycopg import AsyncConnection

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.db.repositories.artist_merges import ARTIST_GROUP_SQL
from backend.domain.curation import CurationStorageError
from backend.domain.version_pages import VersionEntryRow, WorkFile
from backend.repositories.version_entries import VersionEntryRepository

_LINE_WORKS = """
    SELECT ti.id AS identity_id, ti.version_text, best.library_file_id AS file_id,
           COALESCE(lf.work_id, r.work_id) AS work_id
      FROM track_identities ti
     CROSS JOIN LATERAL (
           SELECT m.library_file_id FROM matches m
            WHERE m.identity_id = ti.id AND m.library_file_id IS NOT NULL
            ORDER BY m.confidence_score DESC, m.created_at, m.id
            LIMIT 1) best
      JOIN library_files lf ON lf.id = best.library_file_id
      LEFT JOIN recordings r ON r.id = lf.recording_id
     WHERE ti.version_text IS NOT NULL
       AND ti.match_status IN ('auto_matched', 'manual_matched')"""

# The scoped variant: %(works)s, an array of work ids. A line's best match is on a file of the
# work (by the file's work or its recording's), so it has some match there: only those
# identities are read, then the view's rule picks the lines that are on the works.
_IN_WORKS = """
       AND ti.id IN (
           SELECT m.identity_id FROM matches m
             JOIN library_files f ON f.id = m.library_file_id
            WHERE f.work_id = ANY(%(works)s)
           UNION
           SELECT m.identity_id FROM matches m
             JOIN library_files f ON f.id = m.library_file_id
             JOIN recordings rr ON rr.id = f.recording_id
            WHERE rr.work_id = ANY(%(works)s))
       AND COALESCE(lf.work_id, r.work_id) = ANY(%(works)s)"""


def _entry_rows_sql(lines: str, entries_on: str) -> str:
    return f"""
WITH lines AS MATERIALIZED ({lines}),
keys AS (
    SELECT work_id, version_text FROM lines WHERE work_id IS NOT NULL
    UNION
    SELECT work_id, version_text FROM format_overrides
     WHERE version_text IS NOT NULL {entries_on}
),
plays AS (
    SELECT l.work_id, l.version_text, COALESCE(sum(sip.play_count), 0) AS plays,
           array_agg(DISTINCT s.call_letters ORDER BY s.call_letters)
               FILTER (WHERE s.call_letters IS NOT NULL) AS stations
      FROM lines l
      LEFT JOIN station_identity_plays sip ON sip.identity_id = l.identity_id
      LEFT JOIN stations s ON s.id = sip.station_id
     GROUP BY l.work_id, l.version_text
),
firsts AS (
    -- Each line's first play, looked up through the line's own plays (play_events by
    -- identity), then the earliest of the text's lines.
    SELECT DISTINCT ON (l.work_id, l.version_text)
           l.work_id, l.version_text,
           (f.played_at AT TIME ZONE 'UTC')::date AS first_seen, f.call_letters AS first_station
      FROM lines l
     CROSS JOIN LATERAL (
           SELECT pe.played_at, s.call_letters
             FROM play_events pe
             JOIN playlists p ON p.id = pe.playlist_id
             JOIN stations s ON s.id = p.station_id
            WHERE pe.identity_id = l.identity_id
            ORDER BY pe.played_at, s.call_letters
            LIMIT 1) f
     ORDER BY l.work_id, l.version_text, f.played_at, f.call_letters
)
SELECT k.work_id, k.version_text, COALESCE(pl.plays, 0) AS plays, pl.stations,
       f.first_seen, f.first_station, fo.preferred_file_id AS mapped_file_id,
       st.state, st.seen_at
  FROM keys k
  LEFT JOIN plays pl ON pl.work_id = k.work_id AND pl.version_text = k.version_text
  LEFT JOIN firsts f ON f.work_id = k.work_id AND f.version_text = k.version_text
  LEFT JOIN format_overrides fo ON fo.work_id = k.work_id AND fo.version_text = k.version_text
  LEFT JOIN version_entry_states st
         ON st.work_id = k.work_id AND st.version_text = k.version_text"""


def _line_files_sql(lines: str) -> str:
    return f"""
SELECT DISTINCT work_id, version_text, file_id FROM ({lines}) lines
 WHERE work_id IS NOT NULL"""


_ENTRY_ROWS_ALL = _entry_rows_sql(_LINE_WORKS, "")
_ENTRY_ROWS_IN = _entry_rows_sql(_LINE_WORKS + _IN_WORKS, "AND work_id = ANY(%(works)s)")
_LINE_FILES_ALL = _line_files_sql(_LINE_WORKS)
_LINE_FILES_IN = _line_files_sql(_LINE_WORKS + _IN_WORKS)

_WORK_FILES = """
SELECT lf.work_id, lf.id AS file_id, r.version_type, lf.indexed_at, lf.track_title, lf.file_path
  FROM library_files lf
  LEFT JOIN recordings r ON r.id = lf.recording_id
 WHERE lf.file_status = 'present' AND lf.work_id = ANY(%(works)s)"""

_MASTER = """
SELECT sm.preferred_file_id, lf.track_title
  FROM song_masters sm
  JOIN library_files lf ON lf.id = sm.preferred_file_id
 WHERE sm.work_id = %(work)s"""

_GROUP_WORKS = f"""
WITH grp AS ({ARTIST_GROUP_SQL})
SELECT w.id FROM works w WHERE w.artist_id IN (SELECT id FROM grp)"""

_ARTIST_EXISTS = "SELECT 1 FROM artists WHERE id = %(id)s"

_WORK_TITLES = """
SELECT w.id, w.title, w.artist_id, a.name AS artist_name
  FROM works w JOIN artists a ON a.id = w.artist_id
 WHERE w.id = ANY(%(works)s)"""

_MAP = """
INSERT INTO format_overrides (work_id, version_text, preferred_file_id)
SELECT %(work)s, t, %(file)s FROM unnest(%(texts)s::text[]) AS t
ON CONFLICT (work_id, version_text) DO UPDATE SET preferred_file_id = EXCLUDED.preferred_file_id"""

_SEE = """
INSERT INTO version_entry_states (work_id, version_text, state, seen_at)
SELECT %(work)s, t, NULL, now() FROM unnest(%(texts)s::text[]) AS t
ON CONFLICT (work_id, version_text) DO UPDATE SET state = NULL, seen_at = now()"""

_UNMAP = """
DELETE FROM format_overrides
 WHERE work_id = %(work)s AND version_text = ANY(%(texts)s::text[])"""

_DISMISS = """
INSERT INTO version_entry_states (work_id, version_text, state, seen_at)
SELECT w, t, 'dismissed', now() FROM unnest(%(works)s::text[], %(texts)s::text[]) AS k(w, t)
ON CONFLICT (work_id, version_text) DO UPDATE SET state = 'dismissed', seen_at = now()"""


Query = tuple[str, dict[str, Any] | None]


def _entry_rows_query(work_ids: list[str] | None) -> Query:
    """Every work's rows (None), or those of *work_ids*."""
    if work_ids is None:
        return _ENTRY_ROWS_ALL, None
    return _ENTRY_ROWS_IN, {"works": work_ids}


def _line_files_query(work_ids: list[str] | None) -> Query:
    if work_ids is None:
        return _LINE_FILES_ALL, None
    return _LINE_FILES_IN, {"works": work_ids}


def _entry_row(row: dict[str, Any]) -> VersionEntryRow:
    mapped = row["mapped_file_id"]
    return VersionEntryRow(
        work_id=row["work_id"],
        version_text=row["version_text"],
        plays=int(row["plays"]),
        stations=tuple(row["stations"] or ()),
        first_seen=row["first_seen"],
        first_station=row["first_station"],
        mapped_file_id=None if mapped is None else UUID(str(mapped)),
        dismissed=row["state"] == "dismissed",
        seen_at=row["seen_at"],
    )


def _line_files(rows: list[dict[str, Any]]) -> dict[tuple[str, str], frozenset[UUID]]:
    found: dict[tuple[str, str], set[UUID]] = defaultdict(set)
    for row in rows:
        found[(row["work_id"], row["version_text"])].add(UUID(str(row["file_id"])))
    return {key: frozenset(files) for key, files in found.items()}


def _work_files(rows: list[dict[str, Any]]) -> dict[str, list[WorkFile]]:
    found: dict[str, list[WorkFile]] = defaultdict(list)
    for row in rows:
        found[row["work_id"]].append(
            WorkFile(
                work_id=row["work_id"],
                file_id=UUID(str(row["file_id"])),
                version_type=row["version_type"],
                indexed_at=row["indexed_at"],
                track_title=row["track_title"],
                file_path=row["file_path"],
            )
        )
    return dict(found)


@translate_lost_connection(CurationStorageError)
class PgVersionEntryRepository(VersionEntryRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def entry_rows(self, work_ids: list[str] | None) -> list[VersionEntryRow]:
        rows = self._conn.execute(*_entry_rows_query(work_ids)).fetchall()
        return [_entry_row(row) for row in rows]

    def line_files(self, work_ids: list[str] | None) -> dict[tuple[str, str], frozenset[UUID]]:
        return _line_files(self._conn.execute(*_line_files_query(work_ids)).fetchall())

    def work_files(self, work_ids: list[str]) -> dict[str, list[WorkFile]]:
        return _work_files(self._conn.execute(_WORK_FILES, {"works": work_ids}).fetchall())

    def group_work_ids(self, artist_id: str) -> list[str] | None:
        if self._conn.execute(_ARTIST_EXISTS, {"id": artist_id}).fetchone() is None:
            return None
        rows = self._conn.execute(_GROUP_WORKS, {"id": artist_id}).fetchall()
        return [str(row["id"]) for row in rows]

    def map_texts(self, work_id: str, texts: list[str], file_id: UUID) -> None:
        params = {"work": work_id, "texts": texts, "file": file_id}
        self._conn.execute(_MAP, params)
        self._conn.execute(_SEE, params)

    def unmap_texts(self, work_id: str, texts: list[str]) -> None:
        self._conn.execute(_UNMAP, {"work": work_id, "texts": texts})

    def dismiss(self, keys: list[tuple[str, str]]) -> None:
        if not keys:
            return
        self._conn.execute(_DISMISS, {"works": [w for w, _ in keys], "texts": [t for _, t in keys]})


class AsyncVersionEntryReader:
    """The read routes' reads, on the request's async connection."""

    def __init__(self, conn: AsyncConnection[Any]) -> None:
        self._conn = conn

    async def entry_rows(self, work_ids: list[str] | None) -> list[VersionEntryRow]:
        cur = await self._conn.execute(*_entry_rows_query(work_ids))
        return [_entry_row(row) for row in await cur.fetchall()]

    async def line_files(
        self, work_ids: list[str] | None
    ) -> dict[tuple[str, str], frozenset[UUID]]:
        cur = await self._conn.execute(*_line_files_query(work_ids))
        return _line_files(await cur.fetchall())

    async def work_files(self, work_ids: list[str]) -> dict[str, list[WorkFile]]:
        cur = await self._conn.execute(_WORK_FILES, {"works": work_ids})
        return _work_files(await cur.fetchall())

    async def master(self, work_id: str) -> tuple[UUID | None, str | None]:
        cur = await self._conn.execute(_MASTER, {"work": work_id})
        row = await cur.fetchone()
        if row is None:
            return None, None
        return UUID(str(row["preferred_file_id"])), row["track_title"]

    async def work_titles(self, work_ids: list[str]) -> dict[str, dict[str, Any]]:
        cur = await self._conn.execute(_WORK_TITLES, {"works": work_ids})
        return {str(row["id"]): dict(row) for row in await cur.fetchall()}
