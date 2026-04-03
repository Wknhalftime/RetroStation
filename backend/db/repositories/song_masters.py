from __future__ import annotations

from typing import Any

import psycopg

from backend.domain.enums import SelectionMethod
from backend.domain.models import SongMaster
from backend.repositories.song_masters import SongMasterRepository


class PgSongMasterRepository(SongMasterRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> SongMaster:
        return SongMaster(
            id=row["id"],
            work_id=row["work_id"],
            preferred_file_id=row["preferred_file_id"],
            selection_method=SelectionMethod(row["selection_method"]),
            score=row.get("score"),
            updated_at=row["updated_at"],
        )

    def upsert(self, master: SongMaster) -> SongMaster:
        self._conn.execute(
            """INSERT INTO song_masters
               (id, work_id, preferred_file_id, selection_method, score, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (work_id) DO UPDATE SET
                 preferred_file_id = EXCLUDED.preferred_file_id,
                 selection_method = EXCLUDED.selection_method,
                 score = EXCLUDED.score,
                 updated_at = EXCLUDED.updated_at
               WHERE song_masters.selection_method = 'auto'""",
            (master.id, master.work_id, master.preferred_file_id,
             master.selection_method.value, master.score, master.updated_at),
        )
        row = self._conn.execute(
            "SELECT * FROM song_masters WHERE work_id = %s", (master.work_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def get_by_work(self, work_id: str) -> SongMaster | None:
        row = self._conn.execute(
            "SELECT * FROM song_masters WHERE work_id = %s", (work_id,)
        ).fetchone()
        return self._row_to_model(row) if row else None

    def list_auto_for_works(self, work_ids: list[str]) -> list[SongMaster]:
        if not work_ids:
            return []
        rows = self._conn.execute(
            "SELECT * FROM song_masters WHERE work_id = ANY(%s) AND selection_method = 'auto'",
            (work_ids,),
        ).fetchall()
        return [self._row_to_model(r) for r in rows]
