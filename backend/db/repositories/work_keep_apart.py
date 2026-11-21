"""PostgreSQL adapter for the "Keep apart" marks (work merges phase 3b PR 5c; SPEC D40; scope
check Q4, Q11; AUD-R089). ``work_keep_apart`` holds each pair in plain string order."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import psycopg
from psycopg.rows import DictRow

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.catalog import CatalogStorageError
from backend.domain.merge_list import KeptApart
from backend.domain.work_merge_planner import WorkPair
from backend.repositories.work_keep_apart import WorkKeepApartRepository


def _row(row: DictRow) -> KeptApart:
    return KeptApart(
        pair=WorkPair(str(row["first_work_id"]), str(row["second_work_id"])),
        kept_at=row["kept_at"],
        note=str(row["note"]),
    )


@translate_lost_connection(CatalogStorageError)
class PgWorkKeepApartRepository(WorkKeepApartRepository):
    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def keep(self, pairs: Sequence[WorkPair], note: str) -> None:
        for pair in pairs:
            self._conn.execute(
                """INSERT INTO work_keep_apart (first_work_id, second_work_id, note)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (pair.first_work_id, pair.second_work_id, note),
            )

    def undo(self, pair: WorkPair) -> None:
        self._conn.execute(
            "DELETE FROM work_keep_apart WHERE first_work_id = %s AND second_work_id = %s",
            (pair.first_work_id, pair.second_work_id),
        )

    def marks(self) -> list[KeptApart]:
        rows = self._conn.execute(
            """SELECT first_work_id, second_work_id, kept_at, note FROM work_keep_apart
               ORDER BY kept_at DESC, first_work_id COLLATE "C", second_work_id COLLATE "C" """
        ).fetchall()
        return [_row(r) for r in rows]

    def pairs_among(self, work_ids: Sequence[str]) -> list[WorkPair]:
        ids = list(dict.fromkeys(work_ids))
        rows = self._conn.execute(
            """SELECT first_work_id, second_work_id FROM work_keep_apart
               WHERE first_work_id = ANY(%(ids)s) AND second_work_id = ANY(%(ids)s)""",
            {"ids": ids},
        ).fetchall()
        return sorted(WorkPair(str(r["first_work_id"]), str(r["second_work_id"])) for r in rows)

    def carry(self, source_ids: Sequence[str], target_id: str) -> None:
        sources = set(source_ids)
        moved = self._conn.execute(
            """DELETE FROM work_keep_apart
               WHERE first_work_id = ANY(%(s)s) OR second_work_id = ANY(%(s)s)
               RETURNING first_work_id, second_work_id, kept_at, note""",
            {"s": sorted(sources)},
        ).fetchall()
        landing: dict[WorkPair, tuple[datetime, str]] = {}
        for row in sorted(moved, key=lambda r: r["kept_at"]):
            ends = [
                target_id if w in sources else w
                for w in (row["first_work_id"], row["second_work_id"])
            ]
            if ends[0] == ends[1]:
                continue
            landing.setdefault(WorkPair.of(*ends), (row["kept_at"], str(row["note"])))
        for pair, (kept_at, note) in landing.items():
            self._conn.execute(
                """INSERT INTO work_keep_apart (first_work_id, second_work_id, kept_at, note)
                   VALUES (%(a)s, %(b)s, %(at)s, %(note)s)
                   ON CONFLICT (first_work_id, second_work_id) DO UPDATE
                      SET kept_at = EXCLUDED.kept_at, note = EXCLUDED.note
                    WHERE EXCLUDED.kept_at < work_keep_apart.kept_at""",
                {"a": pair.first_work_id, "b": pair.second_work_id, "at": kept_at, "note": note},
            )
