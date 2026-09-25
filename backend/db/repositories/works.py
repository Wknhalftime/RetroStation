from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import psycopg
import structlog

from backend.db.repositories._pg_utils import format_embedding, parse_embedding
from backend.domain.catalog import Work, WorkFootprint
from backend.domain.enums import CatalogSource, TargetType
from backend.repositories.works import WorkRepository

logger = structlog.get_logger()


class PgWorkRepository(WorkRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> Work:
        return Work(
            id=row["id"],
            title=row["title"],
            artist_id=row["artist_id"],
            needs_enhancement=row["needs_enhancement"],
            enhanced_at=row.get("enhanced_at"),
            enhancement_error=row.get("enhancement_error"),
            embedding=parse_embedding(row.get("embedding")),
            mbid=row.get("mbid"),
            origin=(
                CatalogSource(row["origin"])
                if row.get("origin")
                else CatalogSource.LOCAL
            ),
        )

    def upsert(self, work: Work) -> Work:
        self._conn.execute(
            """INSERT INTO works (id, title, artist_id)
               VALUES (%s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                 title = EXCLUDED.title""",
            (work.id, work.title, work.artist_id),
        )
        row = self._conn.execute(
            "SELECT * FROM works WHERE id = %s", (work.id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_id(self, mbid: str) -> Work | None:
        row = self._conn.execute(
            "SELECT * FROM works WHERE id = %s", (mbid,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_artist(self, artist_id: str) -> list[Work]:
        rows = self._conn.execute(
            "SELECT * FROM works WHERE artist_id = %s", (artist_id,)
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

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

    def create_local(self, title: str, artist_id: str) -> str:
        work_id = str(uuid4())
        self._conn.execute(
            """INSERT INTO works
                   (id, title, artist_id, origin, needs_enhancement)
               VALUES (%s, %s, %s, 'local', FALSE)""",
            (work_id, title, artist_id),
        )
        return work_id

    def upsert_from_mb(
        self, mbid: str, title: str, artist_id: str,
    ) -> str:
        row = self._conn.execute(
            "SELECT id FROM works WHERE mbid = %s FOR UPDATE",
            (mbid,),
        ).fetchone()
        if row is not None:
            return cast(str, row["id"])
        work_id = str(uuid4())
        self._conn.execute(
            """INSERT INTO works
                   (id, title, artist_id, mbid, origin,
                    needs_enhancement)
               VALUES (%s, %s, %s, %s, 'musicbrainz', TRUE)""",
            (work_id, title, artist_id, mbid),
        )
        return work_id


    def delete_if_empty(self, work_id: str) -> bool:
        count = self._conn.execute(
            "SELECT count(*) AS cnt FROM library_files"
            " WHERE work_id = %s",
            (work_id,),
        ).fetchone()
        if count and count["cnt"] > 0:
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

    def get_candidates_by_normalized_artist(
        self, normalized_artist_name: str, limit: int = 100,
    ) -> list[tuple[str, str]]:
        # Join through `artists.normalized_name` so orphan works (no attached
        # library_files yet) still appear as fuzzy-match candidates — this is
        # what prevents a second, byte-identical work from being created when
        # local grouping can't find the first. `artists.normalized_name` has
        # a unique index (migration 0011) so the lookup is O(log n).
        rows = self._conn.execute(
            """SELECT w.id, w.title
               FROM works w
               JOIN artists a ON a.id = w.artist_id
               WHERE a.normalized_name = %s
               ORDER BY w.title
               LIMIT %s""",
            (normalized_artist_name, limit),
        ).fetchall()
        if len(rows) >= limit:
            logger.warning(
                "Candidate cap hit for artist %s (limit=%d)",
                normalized_artist_name, limit,
            )
        return [(r["id"], r["title"]) for r in rows]

    def list_local_footprints(self) -> list[WorkFootprint]:
        rows = self._conn.execute(
            """SELECT w.id, w.title, w.artist_id,
                      COALESCE(f.cnt, 0) AS file_count,
                      COALESCE(m.cnt, 0) AS match_count
               FROM works w
               LEFT JOIN (SELECT work_id, count(*) AS cnt FROM library_files
                          GROUP BY work_id) f ON f.work_id = w.id
               LEFT JOIN (SELECT work_id, count(*) AS cnt FROM matches
                          GROUP BY work_id) m ON m.work_id = w.id
               WHERE w.origin = 'local'
               ORDER BY w.id"""
        ).fetchall()
        return [
            WorkFootprint(
                id=r["id"],
                title=r["title"],
                artist_id=r["artist_id"],
                file_count=r["file_count"],
                match_count=r["match_count"],
            )
            for r in rows
        ]

    def merge_into(self, target_id: str, source_ids: tuple[str, ...]) -> None:
        # Lock every row in the group so a concurrent grouping pass cannot
        # attach a file to a source between the re-point and the delete.
        locked = self._conn.execute(
            "SELECT id FROM works WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            ([target_id, *source_ids],),
        ).fetchall()
        sources = [r["id"] for r in locked if r["id"] != target_id]
        if not sources:
            return
        self._merge_recordings(target_id, sources)
        self._conn.execute(
            "UPDATE library_files SET work_id = %s WHERE work_id = ANY(%s)",
            (target_id, sources),
        )
        self._conn.execute(
            "UPDATE matches SET work_id = %s WHERE work_id = ANY(%s)",
            (target_id, sources),
        )
        self._conn.execute(
            """UPDATE matches SET target_id = %s
               WHERE target_type = %s AND target_id = ANY(%s)""",
            (target_id, TargetType.WORK.value, sources),
        )
        self._merge_format_overrides(target_id, sources)
        self._conn.execute(
            "DELETE FROM song_masters WHERE work_id = ANY(%s)", (sources,),
        )
        self._conn.execute("DELETE FROM works WHERE id = ANY(%s)", (sources,))

    def _merge_recordings(self, target_id: str, sources: list[str]) -> None:
        """Move source recordings to the target.

        ``uq_recordings_work_version`` allows one recording per version type,
        so a source recording whose version the target already has hands its
        files to the target's recording and is deleted instead.
        """
        clashes = self._conn.execute(
            """SELECT s.id AS source_rec, t.id AS target_rec
               FROM recordings s
               JOIN recordings t
                 ON t.work_id = %s AND t.version_type = s.version_type
               WHERE s.work_id = ANY(%s)""",
            (target_id, sources),
        ).fetchall()
        for clash in clashes:
            self._conn.execute(
                "UPDATE library_files SET recording_id = %s WHERE recording_id = %s",
                (clash["target_rec"], clash["source_rec"]),
            )
            self._conn.execute(
                "DELETE FROM recordings WHERE id = %s", (clash["source_rec"],),
            )
        # Two sources can share a version the target lacks; the oldest-id one
        # moves and the rest collapse onto it.
        keepers = self._conn.execute(
            """SELECT DISTINCT ON (version_type) id, version_type
               FROM recordings WHERE work_id = ANY(%s)
               ORDER BY version_type, id""",
            (sources,),
        ).fetchall()
        for keeper in keepers:
            dupes = self._conn.execute(
                """SELECT id FROM recordings
                   WHERE work_id = ANY(%s) AND version_type = %s AND id <> %s""",
                (sources, keeper["version_type"], keeper["id"]),
            ).fetchall()
            for dupe in dupes:
                self._conn.execute(
                    "UPDATE library_files SET recording_id = %s WHERE recording_id = %s",
                    (keeper["id"], dupe["id"]),
                )
                self._conn.execute("DELETE FROM recordings WHERE id = %s", (dupe["id"],))
            self._conn.execute(
                "UPDATE recordings SET work_id = %s WHERE id = %s",
                (target_id, keeper["id"]),
            )

    def _merge_format_overrides(self, target_id: str, sources: list[str]) -> None:
        """Move source format overrides, honoring UNIQUE (work_id, format_name).

        The target's own override wins a clash; between sources the oldest
        wins (id tie-break). Losers are deleted.
        """
        self._conn.execute(
            """DELETE FROM format_overrides
               WHERE work_id = ANY(%(src)s)
                 AND (
                   format_name IN (SELECT format_name FROM format_overrides
                                   WHERE work_id = %(tgt)s)
                   OR id NOT IN (
                     SELECT DISTINCT ON (format_name) id FROM format_overrides
                     WHERE work_id = ANY(%(src)s)
                     ORDER BY format_name, created_at ASC, id ASC
                   )
                 )""",
            {"src": sources, "tgt": target_id},
        )
        self._conn.execute(
            "UPDATE format_overrides SET work_id = %s WHERE work_id = ANY(%s)",
            (target_id, sources),
        )
