from __future__ import annotations

from typing import Any

import psycopg

from backend.domain.enums import VersionType
from backend.domain.models import Recording
from backend.repositories.recordings import RecordingRepository


class PgRecordingRepository(RecordingRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> Recording:
        return Recording(
            id=row["id"],
            title=row["title"],
            work_id=row.get("work_id"),
            duration_ms=row.get("duration_ms"),
            version_type=(
                VersionType(row["version_type"])
                if row.get("version_type")
                else VersionType.ORIGINAL
            ),
            needs_enhancement=row["needs_enhancement"],
            enhanced_at=row.get("enhanced_at"),
            enhancement_error=row.get("enhancement_error"),
            embedding=(
                [float(x) for x in row["embedding"].strip("[]").split(",")]
                if row.get("embedding")
                else None
            ),
        )

    def upsert(self, recording: Recording) -> Recording:
        self._conn.execute(
            """INSERT INTO recordings (id, title, work_id, duration_ms, version_type)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
                 title = EXCLUDED.title""",
            (recording.id, recording.title, recording.work_id,
             recording.duration_ms, recording.version_type.value),
        )
        row = self._conn.execute(
            "SELECT * FROM recordings WHERE id = %s", (recording.id,)
        ).fetchone()
        assert row is not None
        return self._row_to_model(row)

    def get_by_id(self, mbid: str) -> Recording | None:
        row = self._conn.execute(
            "SELECT * FROM recordings WHERE id = %s", (mbid,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def get_by_work(self, work_id: str) -> list[Recording]:
        rows = self._conn.execute(
            "SELECT * FROM recordings WHERE work_id = %s", (work_id,)
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def update_embedding(self, mbid: str, embedding: list[float]) -> None:
        self._conn.execute(
            "UPDATE recordings SET embedding = %s WHERE id = %s",
            ("[" + ",".join(str(v) for v in embedding) + "]", mbid),
        )
