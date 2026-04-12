from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import psycopg

from backend.domain.catalog import Work
from backend.domain.enums import CatalogSource
from backend.repositories.works import WorkRepository

logger = logging.getLogger(__name__)


def _parse_embedding(raw: Any) -> list[float] | None:
    """Convert a pgvector embedding column value to a list of floats.

    pgvector may return the value as a Python list (already parsed) or as a
    bracketed string such as ``"[0.1,0.2,0.3]"``.  Both cases are handled
    gracefully; ``None`` is returned when the column is NULL.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    return [float(x) for x in str(raw).strip("[]").split(",")]


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
            embedding=_parse_embedding(row.get("embedding")),
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
        rows = self._conn.execute(
            "SELECT * FROM works WHERE needs_enhancement = TRUE"
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
            ("[" + ",".join(str(v) for v in embedding) + "]", mbid),
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
            return row["id"]
        work_id = str(uuid4())
        self._conn.execute(
            """INSERT INTO works
                   (id, title, artist_id, mbid, origin,
                    needs_enhancement)
               VALUES (%s, %s, %s, %s, 'musicbrainz', TRUE)""",
            (work_id, title, artist_id, mbid),
        )
        return work_id

    def get_by_mbid(self, mbid: str) -> Work | None:
        row = self._conn.execute(
            "SELECT * FROM works WHERE mbid = %s", (mbid,),
        ).fetchone()
        return self._row_to_model(row) if row else None

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
        rows = self._conn.execute(
            """SELECT DISTINCT w.id, w.title
               FROM works w
               JOIN library_files lf ON lf.work_id = w.id
               WHERE lf.normalized_artist_name = %s
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
