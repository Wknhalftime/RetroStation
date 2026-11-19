"""PostgreSQL adapter for the global recording index (work merges phase 3b PR 4; SPEC S5).

Runs on an autocommit connection: ``CREATE INDEX CONCURRENTLY`` can't run in a transaction, and
it waits for every transaction open when it starts.
"""

from __future__ import annotations

from typing import Any

import psycopg

from backend.domain.performances import RecordingIndexDuplicateError, RecordingIndexState
from backend.repositories.recording_index import RecordingIndexRepository

INDEX = "uq_performances_recording_mbid"


class PgRecordingIndexRepository(RecordingIndexRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        if not conn.autocommit:
            raise ValueError("the recording index needs an autocommit connection")
        self._conn = conn

    def state(self) -> RecordingIndexState:
        row = self._conn.execute(
            """SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
               WHERE c.relname = %s""",
            (INDEX,),
        ).fetchone()
        if row is None:
            return RecordingIndexState.ABSENT
        valid = row["indisvalid"] if isinstance(row, dict) else row[0]
        return RecordingIndexState.VALID if valid else RecordingIndexState.INVALID

    def drop(self) -> None:
        self._conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")

    def build(self) -> None:
        try:
            self._conn.execute(
                f"""CREATE UNIQUE INDEX CONCURRENTLY {INDEX}
                    ON performances (recording_mbid) WHERE recording_mbid IS NOT NULL"""
            )
        except psycopg.errors.UniqueViolation as duplicate:
            raise RecordingIndexDuplicateError(str(duplicate)) from duplicate
