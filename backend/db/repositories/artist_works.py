"""The artist pages' works reads, on the request's async connection (artist model phase 3b
PR 5b; SPEC S7, scope note R6, R9: they left ``routers/library/artists.py``, which S7 rewrote).
"""

from __future__ import annotations

from collections import defaultdict
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

# The list's works read (its work and file counts, and its new-version counts' works) is three
# small reads, each sized by its array, so the planner takes the indexes: the page's groups, their
# credits, the files on their works. One query filtering after _WORK_ARTISTS built every credit
# pair took 325 ms a page on the dev copy, and one joining the merge walk into the credits still
# planned for every credit and every file, the walk's estimate being far too high (SPEC D53).

# Each group on the page: a root merged into no one, and the members whose root it is (the one
# merge walk, MERGE_ROOTS_CTE, 2a S11), with each member's MBID.
_PAGE_GROUPS_SQL = f"""
    WITH RECURSIVE {MERGE_ROOTS_CTE}
    SELECT a.id AS artist_id, a.id AS root_id, a.mbid
      FROM artists a
     WHERE a.id = ANY(%(ids)s)
       AND NOT EXISTS (SELECT 1 FROM merge_roots merged WHERE merged.artist_id = a.id)
    UNION ALL
    SELECT r.artist_id, r.primary_id, a.mbid
      FROM merge_roots r JOIN artists a ON a.id = r.artist_id
     WHERE r.primary_id = ANY(%(ids)s)
"""

# The works the members are credited on: by artist id, and by MBID for a credit no artist id
# names (3a's D1; the MBID's one holder is the member).
_MEMBER_CREDITS_SQL = """
    SELECT pc.artist_id, NULL::text AS mbid, p.work_id
      FROM performance_credits pc JOIN performances p ON p.id = pc.performance_id
     WHERE pc.artist_id = ANY(%(artists)s)
    UNION ALL
    SELECT NULL::text, pc.mbid, p.work_id
      FROM performance_credits pc JOIN performances p ON p.id = pc.performance_id
     WHERE pc.artist_id IS NULL AND pc.mbid = ANY(%(mbids)s)
"""

# The files on each work; a file is on one work, so a group's files are its works' sum.
_WORK_FILES_SQL = """
    SELECT work_id, count(*) AS files FROM library_files
     WHERE work_id = ANY(%(works)s) GROUP BY work_id
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
        cur = await self._conn.execute(_PAGE_GROUPS_SQL, {"ids": root_ids})
        members = await cur.fetchall()
        root_of = {str(m["artist_id"]): str(m["root_id"]) for m in members}
        root_of_mbid = {str(m["mbid"]): str(m["root_id"]) for m in members if m["mbid"]}
        cur = await self._conn.execute(
            _MEMBER_CREDITS_SQL, {"artists": list(root_of), "mbids": list(root_of_mbid)}
        )
        works: dict[str, set[str]] = defaultdict(set)
        for row in await cur.fetchall():
            artist = row["artist_id"]
            root = root_of[str(artist)] if artist is not None else root_of_mbid[str(row["mbid"])]
            works[root].add(str(row["work_id"]))
        every = sorted({w for found in works.values() for w in found})
        files: dict[str, int] = {}
        if every:
            cur = await self._conn.execute(_WORK_FILES_SQL, {"works": every})
            files = {str(r["work_id"]): int(r["files"]) for r in await cur.fetchall()}
        return {
            root: ListedWorks(
                work_ids=tuple(sorted(found)), file_count=sum(files.get(w, 0) for w in found)
            )
            for root, found in works.items()
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
