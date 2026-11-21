"""The artist pages' works reads, on the request's async connection (artist model phase 3b
PR 5b; SPEC S7, scope note R6, R9: they left ``routers/library/artists.py``, which S7 rewrote).
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from backend.db.repositories.artist_merges import MERGE_ROOTS_CTE
from backend.domain.artist_page import GroupWork, ListedWorks
from backend.repositories.artist_works import ArtistWorksReader

# Each (work, credited artist): a work's artists are its performances' credits, a credit by MBID
# alone naming the artist that holds the MBID (3b S7, R6). A work with no performance has none.
_WORK_ARTISTS = """
    SELECT DISTINCT p.work_id, COALESCE(pc.artist_id, held.id) AS artist_id
      FROM performances p
      JOIN performance_credits pc ON pc.performance_id = p.id
      LEFT JOIN artists held ON pc.artist_id IS NULL AND held.mbid = pc.mbid
     WHERE COALESCE(pc.artist_id, held.id) IS NOT NULL
"""

# A page of listed artists' works, each work under the root of each credited artist's group,
# and the files on them (the list's work and file counts, and its new-version counts' works).
_LISTED_WORKS_SQL = f"""
    WITH RECURSIVE {MERGE_ROOTS_CTE}
    SELECT w.root_id, array_agg(DISTINCT w.work_id) AS work_ids,
           COUNT(DISTINCT lf.id) AS file_count
      FROM (SELECT DISTINCT COALESCE(r.primary_id, wa.artist_id) AS root_id, wa.work_id
              FROM ({_WORK_ARTISTS}) wa
              LEFT JOIN merge_roots r ON r.artist_id = wa.artist_id
             WHERE COALESCE(r.primary_id, wa.artist_id) = ANY(%(ids)s)) w
      LEFT JOIN library_files lf ON lf.work_id = w.work_id
     GROUP BY w.root_id
"""

# The works any member of the group is credited on (3b S7, R6), with the members credited.
_GROUP_WORKS_SQL = f"""
    SELECT
        w.id,
        w.title,
        w.mbid,
        w.origin,
        array_agg(DISTINCT wa.artist_id) AS credited,
        COUNT(DISTINCT lf.id) AS recording_count,
        COUNT(DISTINCT sm.id) AS master_count,
        array_agg(DISTINCT r.version_type)
            FILTER (WHERE r.version_type IS NOT NULL)
            AS version_types
    FROM works w
    JOIN ({_WORK_ARTISTS}) wa ON wa.work_id = w.id AND wa.artist_id = ANY(%s)
    LEFT JOIN library_files lf ON lf.work_id = w.id
    LEFT JOIN song_masters sm ON sm.work_id = w.id
    LEFT JOIN recordings r ON r.work_id = w.id
    GROUP BY w.id, w.title, w.mbid, w.origin
    ORDER BY w.title
"""


class AsyncArtistWorksReader(ArtistWorksReader):
    """The artist pages' works reads, on the request's async connection."""

    def __init__(self, conn: AsyncConnection[Any]) -> None:
        self._conn = conn

    async def listed_works(self, root_ids: list[str]) -> dict[str, ListedWorks]:
        if not root_ids:
            return {}
        cur = await self._conn.execute(_LISTED_WORKS_SQL, {"ids": root_ids})
        return {
            str(row["root_id"]): ListedWorks(
                work_ids=tuple(str(w) for w in row["work_ids"]), file_count=row["file_count"]
            )
            for row in await cur.fetchall()
        }

    async def group_works(self, member_ids: list[str]) -> list[GroupWork]:
        cur = await self._conn.execute(_GROUP_WORKS_SQL, (member_ids,))
        return [
            GroupWork(
                id=row["id"],
                title=row["title"],
                mbid=row.get("mbid"),
                origin=row.get("origin") or "local",
                recording_count=row["recording_count"],
                has_master=row["master_count"] > 0,
                version_types=tuple(row.get("version_types") or ()),
                credited=frozenset(str(a) for a in row["credited"] or ()),
            )
            for row in await cur.fetchall()
        ]
