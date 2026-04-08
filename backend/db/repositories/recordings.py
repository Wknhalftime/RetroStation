from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg

from backend.domain.enums import VersionType
from backend.domain.models import Recording
from backend.repositories.recordings import RecordingRepository


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
            embedding=_parse_embedding(row.get("embedding")),
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
        if row is None:
            raise RuntimeError("Row not found after INSERT")
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

    def list_needing_enhancement(self) -> list[Recording]:
        rows = self._conn.execute(
            "SELECT * FROM recordings WHERE needs_enhancement = TRUE"
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def mark_enhanced(self, mbid: str) -> None:
        self._conn.execute(
            "UPDATE recordings SET needs_enhancement = FALSE, enhanced_at = now()"
            " WHERE id = %s",
            (mbid,),
        )

    def get_or_create_local(
        self, work_id: str, version_type: str, title: str,
    ) -> str:
        recording_id = str(uuid4())
        cur = self._conn.execute(
            """INSERT INTO recordings
                   (id, title, work_id, version_type, needs_enhancement)
               VALUES (%s, %s, %s, %s, FALSE)
               ON CONFLICT (work_id, version_type) DO NOTHING
               RETURNING id""",
            (recording_id, title, work_id, version_type),
        )
        row = cur.fetchone()
        if row is not None:
            return row["id"]
        # Row already existed — fetch the winner
        existing = self._conn.execute(
            "SELECT id FROM recordings"
            " WHERE work_id = %s AND version_type = %s",
            (work_id, version_type),
        ).fetchone()
        if existing is None:
            raise RuntimeError(
                "Recording not found after ON CONFLICT DO NOTHING"
            )
        return existing["id"]
