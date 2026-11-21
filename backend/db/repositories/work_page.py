"""The work page's reads, on the request's async connection (artist model phase 3b PR 5a; SPEC
S8, D41; design §8's 3b exit: no SQL left in ``routers/library/works.py``).

The detail's SQL moved here from the router as it was; the performances, their credits and
copies, the work's lines and other works' credited artists are new. The lines are read through
the air-play view, one play per (station, line) standing for them all, fenced with
``LATERAL ... OFFSET 0`` like its other readers.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from backend.domain.enums import FileStatus
from backend.domain.work_page import (
    CreditName,
    OtherWork,
    PageFile,
    PageMaster,
    PageOverride,
    PageRecording,
    PerformanceFacts,
    PlaySource,
    WorkHead,
    WorkLine,
)

_WORK = """SELECT id, title, artist_id FROM works
           WHERE id = COALESCE((SELECT new_id FROM work_redirects WHERE old_id = %s), %s)"""

_RECORDINGS = """
SELECT r.id AS rec_id, r.title AS rec_title, r.version_type AS rec_version_type,
       r.duration_ms AS rec_duration_ms,
       lf.id AS file_id, lf.file_path, lf.format, lf.bitrate, lf.duration_ms AS file_duration_ms,
       lf.track_title, lf.release_title, lf.enrichment_status, lf.file_status,
       lf.work_id AS file_work_id, fw.title AS file_work_title, fa.name AS file_work_artist_name
  FROM recordings r
  LEFT JOIN library_files lf ON lf.recording_id = r.id
  LEFT JOIN works fw ON fw.id = lf.work_id
  LEFT JOIN artists fa ON fa.id = fw.artist_id
 WHERE r.id IN (
     SELECT id FROM recordings WHERE work_id = %s
     UNION
     SELECT recording_id FROM library_files WHERE work_id = %s AND recording_id IS NOT NULL
 )
 ORDER BY r.id, lf.file_path
"""

_ORPHANS = """
SELECT lf.id AS file_id, lf.file_path, lf.format, lf.bitrate, lf.duration_ms, lf.track_title,
       lf.release_title, lf.enrichment_status, lf.file_status
  FROM library_files lf
 WHERE lf.work_id = %s AND lf.recording_id IS NULL
 ORDER BY lf.file_path
"""

_MASTER = "SELECT * FROM song_masters WHERE work_id = %s"

_OVERRIDES = """SELECT * FROM format_overrides WHERE work_id = %s AND format_name IS NOT NULL
                ORDER BY format_name"""

_SYNTHETIC_FILES = """
SELECT lf.id AS file_id, lf.file_path, lf.format, lf.bitrate, lf.duration_ms, lf.track_title,
       lf.release_title, lf.enrichment_status, lf.file_status, lf.recording_id
  FROM library_files lf
 WHERE (lf.album_artist_mbid = %s OR lf.artist_mbid = %s) AND lf.track_title = %s
 ORDER BY lf.file_path
"""

_PERFORMANCES = """SELECT id, version_type, preferred_copy_id FROM performances
                   WHERE work_id = %s ORDER BY id"""

# A credit by MBID alone names the artist holding that MBID, if any (3a's D1).
_CREDITS = """
SELECT pc.performance_id, COALESCE(pc.artist_id, held.id) AS artist_id,
       COALESCE(pc.credited_name, a.name, held.name, pc.mbid) AS name, pc.join_after
  FROM performance_credits pc
  LEFT JOIN artists a ON a.id = pc.artist_id
  LEFT JOIN artists held ON pc.artist_id IS NULL AND held.mbid = pc.mbid
 WHERE pc.performance_id = ANY(%s)
 ORDER BY pc.performance_id, pc.position
"""

_COPIES = """
SELECT lf.id AS file_id, lf.performance_id, lf.file_path, lf.format, lf.bitrate, lf.duration_ms,
       lf.track_title, lf.release_title, lf.enrichment_status, lf.file_status
  FROM library_files lf
 WHERE lf.work_id = %s AND lf.performance_id IS NOT NULL
 ORDER BY lf.file_path, lf.id
"""

# Each other work's credited names, its master's performance first.
_OTHER_ARTISTS = """
SELECT p.work_id, COALESCE(pc.credited_name, a.name, held.name, pc.mbid) AS name
  FROM performances p
  JOIN performance_credits pc ON pc.performance_id = p.id
  LEFT JOIN artists a ON a.id = pc.artist_id
  LEFT JOIN artists held ON pc.artist_id IS NULL AND held.mbid = pc.mbid
  LEFT JOIN song_masters sm ON sm.work_id = p.work_id
  LEFT JOIN library_files mf ON mf.id = sm.preferred_file_id
 WHERE p.work_id = ANY(%s)
 ORDER BY p.work_id, (mf.performance_id = p.id) DESC NULLS LAST, p.id, pc.position
"""

# The lines whose best match's file is on the work, and each station's resolution of one of its
# plays (the guarded merge's rule, ``work_merges.py``). The candidates are a UNION of the two
# indexed paths to the work's files (SPEC D48): one OR across the joined tables scanned both.
_LINES = """
WITH candidates AS (
    SELECT m.identity_id
      FROM library_files f
      JOIN matches m ON m.library_file_id = f.id
     WHERE f.work_id = %(work)s
    UNION
    SELECT m.identity_id
      FROM recordings r
      JOIN library_files f ON f.recording_id = r.id
      JOIN matches m ON m.library_file_id = f.id
     WHERE r.work_id = %(work)s
), lines AS (
    SELECT ti.id, ti.original_title, ti.broadcast_artist_id, best.pinned_file_id
      FROM candidates c
      JOIN track_identities ti ON ti.id = c.identity_id
     CROSS JOIN LATERAL (
         SELECT m.library_file_id, m.pinned_file_id
           FROM matches m
          WHERE m.identity_id = ti.id
            AND m.library_file_id IS NOT NULL
            AND ti.match_status IN ('auto_matched', 'manual_matched')
          ORDER BY m.confidence_score DESC, m.created_at, m.id
          LIMIT 1
     ) best
      JOIN library_files f ON f.id = best.library_file_id
      LEFT JOIN recordings r ON r.id = f.recording_id
     WHERE COALESCE(f.work_id, r.work_id) = %(work)s
)
SELECT l.id AS identity_id, l.original_title AS title, ba.original_name AS artist,
       sip.station_id, st.call_letters AS station, st.format_name, sip.play_count AS plays,
       v.file_id, v.source, pf.file_path AS path, l.pinned_file_id
  FROM lines l
  JOIN broadcast_artists ba ON ba.id = l.broadcast_artist_id
  JOIN station_identity_plays sip ON sip.identity_id = l.id AND sip.play_count > 0
  JOIN stations st ON st.id = sip.station_id
 CROSS JOIN LATERAL (
     SELECT pe.id FROM play_events pe JOIN playlists pl ON pl.id = pe.playlist_id
      WHERE pe.identity_id = l.id AND pl.station_id = sip.station_id
      LIMIT 1
 ) one
 CROSS JOIN LATERAL (
     SELECT r.file_id, r.source FROM play_file_resolution r
      WHERE r.play_event_id = one.id OFFSET 0
 ) v
  LEFT JOIN library_files pf ON pf.id = v.file_id
"""


def _uuid(value: object) -> UUID | None:
    return None if value is None else UUID(str(value))


def _file(row: dict[str, Any], duration: str, other: OtherWork | None = None) -> PageFile:
    return PageFile(
        id=UUID(str(row["file_id"])),
        file_path=row["file_path"],
        format=row["format"],
        bitrate=row.get("bitrate"),
        duration_ms=row.get(duration),
        track_title=row.get("track_title"),
        release_title=row.get("release_title"),
        enrichment_status=row["enrichment_status"],
        file_status=FileStatus(row["file_status"]),
        other_work=other,
    )


class AsyncWorkPageReader:
    """The work page's reads, on the request's async connection."""

    def __init__(self, conn: AsyncConnection[Any]) -> None:
        self._conn = conn

    async def work(self, work_id: str) -> WorkHead | None:
        cur = await self._conn.execute(_WORK, (work_id, work_id))
        row = await cur.fetchone()
        if row is None:
            return None
        return WorkHead(id=str(row["id"]), title=row["title"], artist_id=str(row["artist_id"]))

    async def exists(self, work_id: str) -> bool:
        cur = await self._conn.execute("SELECT 1 FROM works WHERE id = %s", (work_id,))
        return await cur.fetchone() is not None

    async def recordings(self, work_id: str, title: str) -> list[PageRecording]:
        cur = await self._conn.execute(_RECORDINGS, (work_id, work_id))
        rows = await cur.fetchall()
        others = await self._other_artists(
            sorted({str(r["file_work_id"]) for r in rows if _elsewhere(r, work_id)})
        )
        found: dict[str, tuple[dict[str, Any], list[PageFile]]] = {}
        for row in rows:
            head, files = found.setdefault(str(row["rec_id"]), (row, []))
            if row["file_id"] is None:
                continue
            other = None
            if _elsewhere(row, work_id):
                other = OtherWork(
                    id=str(row["file_work_id"]),
                    title=row["file_work_title"],
                    artist_name=row["file_work_artist_name"],
                    artists=others.get(str(row["file_work_id"]), ()),
                )
            files.append(_file(row, "file_duration_ms", other))
        recordings = [
            PageRecording(
                id=rec_id,
                title=head["rec_title"],
                version_type=head["rec_version_type"] or "original",
                duration_ms=head["rec_duration_ms"],
                files=tuple(files),
            )
            for rec_id, (head, files) in found.items()
        ]
        cur = await self._conn.execute(_ORPHANS, (work_id,))
        orphans = [_file(row, "duration_ms") for row in await cur.fetchall()]
        if orphans:
            recordings.append(
                PageRecording(f"orphan:{work_id}", title, "original", None, tuple(orphans))
            )
        return recordings

    async def _other_artists(self, work_ids: list[str]) -> dict[str, tuple[str, ...]]:
        if not work_ids:
            return {}
        cur = await self._conn.execute(_OTHER_ARTISTS, (work_ids,))
        names: dict[str, list[str]] = defaultdict(list)
        for row in await cur.fetchall():
            if row["name"] not in names[str(row["work_id"])]:
                names[str(row["work_id"])].append(row["name"])
        return {w: tuple(n) for w, n in names.items()}

    async def song_master(self, work_id: str) -> PageMaster | None:
        cur = await self._conn.execute(_MASTER, (work_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        return PageMaster(
            id=UUID(str(row["id"])),
            preferred_file_id=UUID(str(row["preferred_file_id"])),
            selection_method=row["selection_method"],
            score=row.get("score"),
            updated_at=row["updated_at"],
        )

    async def format_overrides(self, work_id: str) -> list[PageOverride]:
        cur = await self._conn.execute(_OVERRIDES, (work_id,))
        return [
            PageOverride(
                id=UUID(str(row["id"])),
                format_name=row["format_name"],
                preferred_file_id=UUID(str(row["preferred_file_id"])),
                notes=row.get("notes"),
                created_at=row["created_at"],
            )
            for row in await cur.fetchall()
        ]

    async def performances(
        self, work_id: str
    ) -> tuple[list[PerformanceFacts], dict[UUID, PageFile]]:
        cur = await self._conn.execute(_PERFORMANCES, (work_id,))
        heads = await cur.fetchall()
        if not heads:
            return [], {}
        ids = [UUID(str(r["id"])) for r in heads]
        cur = await self._conn.execute(_CREDITS, (ids,))
        credits: dict[UUID, list[CreditName]] = defaultdict(list)
        for row in await cur.fetchall():
            credits[UUID(str(row["performance_id"]))].append(
                CreditName(row["artist_id"], str(row["name"]), row["join_after"])
            )
        cur = await self._conn.execute(_COPIES, (work_id,))
        copies: dict[UUID, PageFile] = {}
        held: dict[UUID, list[UUID]] = defaultdict(list)
        for row in await cur.fetchall():
            copy = _file(row, "duration_ms")
            copies[copy.id] = copy
            held[UUID(str(row["performance_id"]))].append(copy.id)
        facts = [
            PerformanceFacts(
                id=performance_id,
                credit=tuple(credits[performance_id]),
                version_type=str(head["version_type"]),
                preferred_copy_id=_uuid(head["preferred_copy_id"]),
                copy_ids=tuple(held[performance_id]),
            )
            for performance_id, head in zip(ids, heads, strict=True)
        ]
        return facts, copies

    async def lines(self, work_id: str) -> list[WorkLine]:
        cur = await self._conn.execute(_LINES, {"work": work_id})
        return [
            WorkLine(
                identity_id=UUID(str(r["identity_id"])),
                title=str(r["title"]),
                artist=str(r["artist"]),
                station_id=UUID(str(r["station_id"])),
                station=str(r["station"]),
                station_format=r["format_name"],
                plays=int(r["plays"]),
                file_id=_uuid(r["file_id"]),
                path=r["path"],
                source=None if r["source"] is None else PlaySource(r["source"]),
                pinned_file_id=_uuid(r["pinned_file_id"]),
            )
            for r in await cur.fetchall()
        ]

    async def synthetic_files(self, artist_mbid: str, title: str) -> list[PageFile]:
        cur = await self._conn.execute(_SYNTHETIC_FILES, (artist_mbid, artist_mbid, title))
        return [_file(row, "duration_ms") for row in await cur.fetchall()]


def _elsewhere(row: dict[str, Any], work_id: str) -> bool:
    return row["file_work_id"] is not None and str(row["file_work_id"]) != work_id
