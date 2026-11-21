from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import uuid4

import psycopg
import structlog

from backend.db.repositories._pg_utils import (
    format_embedding,
    parse_embedding,
    translate_lost_connection,
)
from backend.domain.catalog import CatalogStorageError, EmptyWork, Work, WorkReference
from backend.domain.enums import CatalogSource, TargetType
from backend.domain.performances import WorkArtist
from backend.repositories.works import WorkRepository

logger = structlog.get_logger()

# What keeps a work with no files (C53, 3b S6), each an EXISTS on the work ``w``; the one
# deletion rule and the kept list read the same SQL.
_REFERENCES: dict[WorkReference, str] = {
    WorkReference.RECORDING: "EXISTS (SELECT 1 FROM recordings r WHERE r.work_id = w.id)",
    WorkReference.WORK_MATCH: (
        "EXISTS (SELECT 1 FROM matches m WHERE m.target_type = %(work)s AND m.target_id = w.id)"
    ),
    WorkReference.FORMAT_OVERRIDE: (
        "EXISTS (SELECT 1 FROM format_overrides o"
        " WHERE o.work_id = w.id AND o.format_name IS NOT NULL)"
    ),
    WorkReference.VERSION_ENTRY: (
        "EXISTS (SELECT 1 FROM format_overrides o"
        " WHERE o.work_id = w.id AND o.version_text IS NOT NULL)"
    ),
    WorkReference.STORED_PERFORMANCE: (
        "EXISTS (SELECT 1 FROM matches m JOIN performances p ON p.id = m.performance_id"
        " WHERE p.work_id = w.id)"
    ),
}

# The (work, artist, position) of every credit, a credit by MBID alone naming the artist that
# holds the MBID (3a's D1): a work's artists are its performances' credits (3b S7). A UNION of
# the two indexed paths, not one OR across the join.
_CREDITED = """
SELECT p.work_id, a.id AS artist_id, a.mbid, a.name, a.normalized_name, pc.position
  FROM performance_credits pc
  JOIN performances p ON p.id = pc.performance_id
  JOIN artists a ON a.id = pc.artist_id
UNION ALL
SELECT p.work_id, a.id, a.mbid, a.name, a.normalized_name, pc.position
  FROM performance_credits pc
  JOIN performances p ON p.id = pc.performance_id
  JOIN artists a ON pc.artist_id IS NULL AND a.mbid = pc.mbid
"""

# The works holding a file of the normalized name (%(name)s) whose performance credits an MBID
# no artist holds: a name with one MBID makes no artist for it (2a D6), so grouping knows such a
# credit by the name its files carry (3b PR 5b's reading of R1). Without it, tagged copies of one
# song would each make a work.
_UNHELD_OF_NAME = """
SELECT f.work_id
  FROM library_files f
  JOIN performance_credits pc ON pc.performance_id = f.performance_id
 WHERE f.normalized_artist_name = %(name)s
   AND pc.artist_id IS NULL
   AND NOT EXISTS (SELECT 1 FROM artists h WHERE h.mbid = pc.mbid)
"""


@translate_lost_connection(CatalogStorageError)
class PgWorkRepository(WorkRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> Work:
        return Work(
            id=row["id"],
            title=row["title"],
            needs_enhancement=row["needs_enhancement"],
            enhanced_at=row.get("enhanced_at"),
            enhancement_error=row.get("enhancement_error"),
            embedding=parse_embedding(row.get("embedding")),
            mbid=row.get("mbid"),
            origin=(CatalogSource(row["origin"]) if row.get("origin") else CatalogSource.LOCAL),
        )

    def upsert(self, work: Work) -> Work:
        self._conn.execute(
            """INSERT INTO works (id, title)
               VALUES (%s, %s)
               ON CONFLICT (id) DO UPDATE SET
                 title = EXCLUDED.title""",
            (work.id, work.title),
        )
        row = self._conn.execute("SELECT * FROM works WHERE id = %s", (work.id,)).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_id(self, mbid: str) -> Work | None:
        row = self._conn.execute("SELECT * FROM works WHERE id = %s", (mbid,)).fetchone()
        return self._row_to_model(row) if row else None

    def works_crediting(self, artist_id: str) -> list[Work]:
        rows = self._conn.execute(
            f"""SELECT * FROM works
                WHERE id IN (SELECT c.work_id FROM ({_CREDITED}) c WHERE c.artist_id = %s)
                ORDER BY id""",  # noqa: S608 (a constant)
            (artist_id,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def credited_artist(self, work_id: str, normalized_name: str) -> WorkArtist | None:
        row = self._conn.execute(
            f"""SELECT c.artist_id, c.mbid, c.name FROM ({_CREDITED}) c
                WHERE c.work_id = %s AND c.normalized_name = %s
                ORDER BY c.position, c.artist_id LIMIT 1""",  # noqa: S608 (a constant)
            (work_id, normalized_name),
        ).fetchone()
        if row is None:
            return None
        return WorkArtist(id=str(row["artist_id"]), mbid=row["mbid"], name=str(row["name"]))

    def list_needing_enhancement(self) -> list[Work]:
        # Mirrors PgArtistRepository.list_unenhanced: rows with a populated
        # enhancement_error are excluded so a permanent failure (once any
        # future code path writes to the column) does not re-queue on every
        # run. Currently no code writes enhancement_error for works, making
        # this a defensive no-op; wired here for parity and forward-safety.
        # Re-queue a row by clearing the column: UPDATE works SET
        # enhancement_error = NULL WHERE id = %s.
        rows = self._conn.execute(
            """SELECT * FROM works
               WHERE needs_enhancement = TRUE
                 AND enhancement_error IS NULL"""
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def mark_enhanced(self, mbid: str) -> None:
        self._conn.execute(
            "UPDATE works SET needs_enhancement = FALSE, enhanced_at = now() WHERE id = %s",
            (mbid,),
        )

    def update_embedding(self, mbid: str, embedding: list[float]) -> None:
        self._conn.execute(
            "UPDATE works SET embedding = %s WHERE id = %s",
            (format_embedding(embedding), mbid),
        )

    def create_local(self, title: str) -> str:
        work_id = str(uuid4())
        self._conn.execute(
            """INSERT INTO works (id, title, origin, needs_enhancement)
               VALUES (%s, %s, 'local', FALSE)""",
            (work_id, title),
        )
        return work_id

    def upsert_from_mb(self, mbid: str, title: str) -> str:
        row = self._conn.execute(
            "SELECT id FROM works WHERE mbid = %s FOR UPDATE",
            (mbid,),
        ).fetchone()
        if row is not None:
            return cast(str, row["id"])
        work_id = str(uuid4())
        self._conn.execute(
            """INSERT INTO works (id, title, mbid, origin, needs_enhancement)
               VALUES (%s, %s, %s, 'musicbrainz', TRUE)""",
            (work_id, title, mbid),
        )
        return work_id

    def list_musicbrainz_ids(self, work_ids: Sequence[str]) -> set[str]:
        if not work_ids:
            return set()
        rows = self._conn.execute(
            "SELECT id FROM works WHERE id = ANY(%s) AND origin = %s",
            (list(work_ids), CatalogSource.MUSICBRAINZ.value),
        ).fetchall()
        return {cast(str, row["id"]) for row in rows}

    def get_id_by_mbid(self, mbid: str) -> str | None:
        row = self._conn.execute("SELECT id FROM works WHERE mbid = %s", (mbid,)).fetchone()
        return cast(str, row["id"]) if row is not None else None

    def claim_mbid(self, work_id: str, mbid: str, title: str) -> bool:
        # works.mbid is UNIQUE; the NOT EXISTS keeps a held MBID a refusal, not a raise.
        cur = self._conn.execute(
            """UPDATE works
               SET mbid = %(mbid)s, origin = %(origin)s, title = %(title)s,
                   needs_enhancement = TRUE
               WHERE id = %(id)s AND origin = %(local)s AND mbid IS NULL
                 AND NOT EXISTS (SELECT 1 FROM works held WHERE held.mbid = %(mbid)s)""",
            {
                "id": work_id,
                "mbid": mbid,
                "title": title,
                "origin": CatalogSource.MUSICBRAINZ.value,
                "local": CatalogSource.LOCAL.value,
            },
        )
        return cur.rowcount == 1

    def drop_fileless_grouping_recordings(self, work_id: str) -> int:
        cur = self._conn.execute(
            """DELETE FROM recordings r
               WHERE r.work_id = %s
                 AND r.needs_enhancement = FALSE
                 AND r.enhanced_at IS NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM library_files lf WHERE lf.recording_id = r.id
                 )""",
            (work_id,),
        )
        return max(0, cur.rowcount)

    def delete_if_empty(self, work_id: str) -> bool:
        # The one deletion rule (C53, 3b S6, D7): files, and every reference the kept list
        # names. song_masters go with the work; version_entry_states and performances
        # cascade (phase 2b D3), unless a match stores one of its performances.
        clauses = " OR ".join(
            [
                "EXISTS (SELECT 1 FROM library_files f WHERE f.work_id = w.id)",
                *_REFERENCES.values(),
            ]
        )
        referenced = self._conn.execute(
            f"SELECT {clauses} AS referenced FROM works w WHERE w.id = %(id)s",
            {"id": work_id, "work": TargetType.WORK.value},
        ).fetchone()
        if referenced is None or referenced["referenced"]:
            return False
        self._conn.execute(
            "DELETE FROM song_masters WHERE work_id = %s",
            (work_id,),
        )
        self._conn.execute(
            "DELETE FROM works WHERE id = %s",
            (work_id,),
        )
        return True

    def list_empty_work_ids(self) -> list[str]:
        rows = self._conn.execute(
            """SELECT w.id FROM works w
               WHERE NOT EXISTS (SELECT 1 FROM library_files f WHERE f.work_id = w.id)
               ORDER BY w.id"""
        ).fetchall()
        return [str(r["id"]) for r in rows]

    def kept_empty_works(self) -> list[EmptyWork]:
        columns = ", ".join(f"{sql} AS {kind.value}" for kind, sql in _REFERENCES.items())
        rows = self._conn.execute(
            f"""SELECT w.id, w.title, {columns} FROM works w
                WHERE NOT EXISTS (SELECT 1 FROM library_files f WHERE f.work_id = w.id)
                ORDER BY w.title, w.id""",
            {"work": TargetType.WORK.value},
        ).fetchall()
        kept = [
            EmptyWork(
                work_id=str(r["id"]),
                title=str(r["title"] or ""),
                references=tuple(kind for kind in _REFERENCES if r[kind.value]),
            )
            for r in rows
        ]
        return [w for w in kept if w.references]

    def get_candidates_by_normalized_artist(
        self,
        normalized_artist_name: str,
        limit: int = 100,
    ) -> list[tuple[str, str]]:
        # The works whose performances credit an artist of the name (3b S7, R1), and those whose
        # files of the name hold a credit no artist holds (_UNHELD_OF_NAME). Names are unique
        # only without an MBID (C41), so this sees the works of every artist of the name (M18);
        # `artists.normalized_name` (0047) and `library_files.normalized_artist_name` (0011)
        # have plain indexes. One IN over a UNION, so both indexed paths drive the read: an OR
        # of two INs scanned every work in parallel, 88 ms a name on the dev copy (SPEC D53).
        rows = self._conn.execute(
            f"""SELECT w.id, w.title
                FROM works w
                WHERE w.id IN (SELECT c.work_id FROM ({_CREDITED}) c
                               WHERE c.normalized_name = %(name)s
                               UNION
                               {_UNHELD_OF_NAME})
                ORDER BY w.title
                LIMIT %(limit)s""",  # noqa: S608 (constants)
            {"name": normalized_artist_name, "limit": limit},
        ).fetchall()
        if len(rows) >= limit:
            logger.warning(
                "Candidate cap hit for artist %s (limit=%d)",
                normalized_artist_name,
                limit,
            )
        return [(r["id"], r["title"]) for r in rows]
