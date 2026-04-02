from __future__ import annotations

from typing import Any

import psycopg

from backend.domain.models import Work
from backend.repositories.works import WorkRepository


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
            embedding=(
                [float(x) for x in row["embedding"].strip("[]").split(",")]
                if row.get("embedding")
                else None
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
        assert row is not None
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
