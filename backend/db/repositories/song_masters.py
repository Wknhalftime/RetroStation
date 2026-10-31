from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.curation import CurationStorageError, SongMaster
from backend.domain.enums import FileStatus, SelectionMethod
from backend.repositories.song_masters import SongMasterRepository


@translate_lost_connection(CurationStorageError)
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
            (
                master.id,
                master.work_id,
                master.preferred_file_id,
                master.selection_method.value,
                master.score,
                master.updated_at,
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM song_masters WHERE work_id = %s", (master.work_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Row not found after INSERT")
        return self._row_to_model(row)

    def replace(self, master: SongMaster) -> None:
        self._conn.execute(
            """INSERT INTO song_masters
               (id, work_id, preferred_file_id, selection_method, score, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (work_id) DO UPDATE SET
                 preferred_file_id = EXCLUDED.preferred_file_id,
                 selection_method = EXCLUDED.selection_method,
                 score = EXCLUDED.score,
                 updated_at = EXCLUDED.updated_at""",
            (
                master.id,
                master.work_id,
                master.preferred_file_id,
                master.selection_method.value,
                master.score,
                master.updated_at,
            ),
        )

    def delete_by_work(self, work_id: str) -> None:
        self._conn.execute("DELETE FROM song_masters WHERE work_id = %s", (work_id,))

    def delete_for_file(self, file_id: UUID) -> list[str]:
        rows = self._conn.execute(
            "DELETE FROM song_masters WHERE preferred_file_id = %s RETURNING work_id",
            (str(file_id),),
        ).fetchall()
        return [r["work_id"] for r in rows]

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

    def list_work_ids_with_missing_master(self) -> list[str]:
        rows = self._conn.execute(
            """SELECT sm.work_id
               FROM song_masters sm
               JOIN library_files master ON master.id = sm.preferred_file_id
               WHERE sm.selection_method = %(auto)s
                 AND master.file_status = %(missing)s
                 AND EXISTS (SELECT 1 FROM library_files lf
                             WHERE lf.work_id = sm.work_id AND lf.file_status = %(present)s)
               ORDER BY sm.work_id""",
            {
                "auto": SelectionMethod.AUTO.value,
                "missing": FileStatus.MISSING.value,
                "present": FileStatus.PRESENT.value,
            },
        ).fetchall()
        return [r["work_id"] for r in rows]
