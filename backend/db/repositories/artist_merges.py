"""PostgreSQL adapter for ``artist_merges`` (phase 2a S11, M33), and the SQL that walks a merge to
its root, which the async Library routes read too."""

from __future__ import annotations

from typing import Any

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.catalog import (
    ArtistGroup,
    ArtistMerge,
    CatalogStorageError,
    MergeArtist,
    MergeArtistNotFoundError,
)
from backend.repositories.artist_merges import ArtistMergeRepository

# Each merged artist and the root primary its chain of edges ends at. It walks down from the
# edges whose primary is merged into nothing, so it reads only artist_merges and always ends.
# Merges take ``lock_merges`` and refuse a cycle, so every chain reaches a root. Use as
# ``WITH RECURSIVE {...}``.
MERGE_ROOTS_CTE = """merge_roots (artist_id, primary_id) AS (
    SELECT m.artist_id, m.primary_id
      FROM artist_merges m
     WHERE NOT EXISTS (SELECT 1 FROM artist_merges up WHERE up.artist_id = m.primary_id)
    UNION ALL
    SELECT m.artist_id, r.primary_id
      FROM artist_merges m
      JOIN merge_roots r ON m.primary_id = r.artist_id
)"""

# The group of the artist ``%(id)s``: each member's id, name, MBID, whether it is the root, and
# the primary it was merged into directly (NULL for the root). No rows for an unknown artist.
# Each member is found by its id, so ``artists`` is read by its primary key only.
ARTIST_GROUP_SQL = f"""WITH RECURSIVE {MERGE_ROOTS_CTE},
root AS (
    SELECT COALESCE((SELECT r.primary_id FROM merge_roots r WHERE r.artist_id = a.id), a.id) AS id
      FROM artists a
     WHERE a.id = %(id)s
),
members AS (
    SELECT root.id FROM root
    UNION ALL
    SELECT r.artist_id FROM merge_roots r JOIN root ON r.primary_id = root.id
)
SELECT a.id, a.name, a.mbid, a.id = root.id AS is_primary, m.primary_id
  FROM members
  JOIN artists a ON a.id = members.id
 CROSS JOIN root
  LEFT JOIN artist_merges m ON m.artist_id = a.id
 ORDER BY a.name, a.id"""


@translate_lost_connection(CatalogStorageError)
class PgArtistMergeRepository(ArtistMergeRepository):
    """The only writer of ``artist_merges``; it commits nothing (the route's request does)."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def lock_merges(self) -> None:
        # SHARE ROW EXCLUSIVE conflicts with itself and with every write of the table, never with
        # a read: one Merge or Unmerge at a time, the list and the pages unblocked.
        self._conn.execute("LOCK TABLE artist_merges IN SHARE ROW EXCLUSIVE MODE")

    def artist(self, artist_id: str) -> MergeArtist | None:
        row = self._conn.execute(
            "SELECT id, name, mbid FROM artists WHERE id = %s", (artist_id,)
        ).fetchone()
        if row is None:
            return None
        return MergeArtist(id=row["id"], name=row["name"], mbid=row["mbid"])

    def primary_of(self, artist_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT primary_id FROM artist_merges WHERE artist_id = %s", (artist_id,)
        ).fetchone()
        return None if row is None else str(row["primary_id"])

    def artist_group(self, artist_id: str) -> ArtistGroup | None:
        rows = self._conn.execute(ARTIST_GROUP_SQL, {"id": artist_id}).fetchall()
        if not rows:
            return None
        return ArtistGroup(
            primary_id=next(str(r["id"]) for r in rows if r["is_primary"]),
            member_ids=frozenset(str(r["id"]) for r in rows),
            member_mbids=frozenset(str(r["mbid"]) for r in rows if r["mbid"]),
        )

    def is_merged(self, artist_id: str) -> bool:
        row = self._conn.execute(
            """SELECT EXISTS (
                   SELECT 1 FROM artist_merges WHERE artist_id = %(id)s OR primary_id = %(id)s
               ) AS merged""",
            {"id": artist_id},
        ).fetchone()
        return bool(row and row["merged"])

    def add_merge(self, merge: ArtistMerge) -> None:
        try:
            self._conn.execute(
                """INSERT INTO artist_merges (artist_id, primary_id, reason)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (artist_id) DO NOTHING""",
                (merge.artist_id, merge.primary_id, merge.reason),
            )
        except psycopg.errors.ForeignKeyViolation as gone:
            # A work merge deleted one of the two since the service read it.
            raise MergeArtistNotFoundError("An artist of the merge no longer exists") from gone

    def remove_merge(self, artist_id: str) -> None:
        self._conn.execute("DELETE FROM artist_merges WHERE artist_id = %s", (artist_id,))
