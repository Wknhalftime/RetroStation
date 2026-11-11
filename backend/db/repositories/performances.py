"""PostgreSQL adapter of :class:`PerformanceRepository` (phase 3a PR 2; migration 0051)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.catalog import CatalogStorageError
from backend.domain.enums import FileStatus, VersionType
from backend.domain.performances import (
    CopyRow,
    CreditPart,
    FileForPerformance,
    NewCredit,
    PerformanceCandidate,
    PerformanceKey,
    StoredLine,
)
from backend.repositories.performances import PerformanceRepository

_KEY_SQL = """
    SELECT p.id, p.recording_mbid, p.version_type, p.length_ms,
           COALESCE(
               (SELECT array_agg(c.mbid) FROM performance_credits c
                 WHERE c.performance_id = p.id AND c.mbid IS NOT NULL),
               ARRAY[]::text[]) AS credit_mbids,
           (SELECT min(f.id::text) FROM library_files f
             WHERE f.performance_id = p.id) AS first_file_id
    FROM performances p
"""


def _uuid(value: object) -> UUID | None:
    return None if value is None else UUID(str(value))


def _key(row: dict[str, Any]) -> PerformanceKey:
    return PerformanceKey(
        recording_mbid=row["recording_mbid"],
        credit_mbids=frozenset(row["credit_mbids"]),
        version_type=VersionType(row["version_type"]),
        length_ms=row["length_ms"],
    )


def _credit(value: object) -> tuple[CreditPart, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        CreditPart(mbid=str(e["mbid"]), name=e.get("name"), join=e.get("join"))
        for e in value
        if isinstance(e, dict) and e.get("mbid")
    )


@translate_lost_connection(CatalogStorageError)
class PgPerformanceRepository(PerformanceRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    # --- reads ---------------------------------------------------------------------------------

    def file_for_performance(self, file_id: UUID) -> FileForPerformance | None:
        row = self._conn.execute(
            """SELECT id, work_id, performance_id, recording_mbid, track_title, duration_ms,
                      credit
               FROM library_files WHERE id = %s""",
            (file_id,),
        ).fetchone()
        if row is None:
            return None
        return FileForPerformance(
            id=UUID(str(row["id"])),
            work_id=row["work_id"],
            performance_id=_uuid(row["performance_id"]),
            recording_mbid=row["recording_mbid"],
            title=row["track_title"],
            length_ms=row["duration_ms"],
            credit=_credit(row["credit"]),
        )

    def candidates(self, work_id: str) -> list[PerformanceCandidate]:
        rows = self._conn.execute(
            _KEY_SQL + " WHERE p.work_id = %s ORDER BY p.id", (work_id,)
        ).fetchall()
        return [
            PerformanceCandidate(
                id=UUID(str(r["id"])), key=_key(r), first_file_id=_uuid(r["first_file_id"])
            )
            for r in rows
        ]

    def held_files(self, file_paths: list[str]) -> list[tuple[FileForPerformance, PerformanceKey]]:
        if not file_paths:
            return []
        rows = self._conn.execute(
            """SELECT f.id AS file_id, f.work_id, f.performance_id, f.recording_mbid AS file_mbid,
                      f.track_title, f.duration_ms, f.credit,
                      k.recording_mbid, k.version_type, k.length_ms, k.credit_mbids
               FROM library_files f
               JOIN ("""
            + _KEY_SQL
            + """) k ON k.id = f.performance_id
               WHERE f.file_path = ANY(%s)
               ORDER BY f.id""",
            (file_paths,),
        ).fetchall()
        return [
            (
                FileForPerformance(
                    id=UUID(str(r["file_id"])),
                    work_id=r["work_id"],
                    performance_id=_uuid(r["performance_id"]),
                    recording_mbid=r["file_mbid"],
                    title=r["track_title"],
                    length_ms=r["duration_ms"],
                    credit=_credit(r["credit"]),
                ),
                _key(r),
            )
            for r in rows
        ]

    def artists_by_mbid(self, mbids: list[str]) -> dict[str, tuple[str, str]]:
        if not mbids:
            return {}
        rows = self._conn.execute(
            "SELECT mbid, id, name FROM artists WHERE mbid = ANY(%s)", (mbids,)
        ).fetchall()
        return {str(r["mbid"]): (str(r["id"]), str(r["name"])) for r in rows}

    def work_of(self, performance_id: UUID) -> str | None:
        row = self._conn.execute(
            "SELECT work_id FROM performances WHERE id = %s", (performance_id,)
        ).fetchone()
        return None if row is None else str(row["work_id"])

    def file_count(self, performance_id: UUID) -> int:
        row = self._conn.execute(
            "SELECT count(*) AS n FROM library_files WHERE performance_id = %s",
            (performance_id,),
        ).fetchone()
        return 0 if row is None else int(row["n"])

    def copies(self, performance_id: UUID) -> list[CopyRow]:
        rows = self._conn.execute(
            """SELECT id, file_status, release_status, release_type, format, bitrate,
                      duration_ms
               FROM library_files WHERE performance_id = %s ORDER BY id""",
            (performance_id,),
        ).fetchall()
        return [
            CopyRow(
                file_id=UUID(str(r["id"])),
                file_status=FileStatus(r["file_status"]),
                release_status=r["release_status"],
                release_type=r["release_type"],
                format=r["format"],
                bitrate=r["bitrate"],
                duration_ms=r["duration_ms"],
            )
            for r in rows
        ]

    def preferred_copy(self, performance_id: UUID) -> UUID | None:
        row = self._conn.execute(
            "SELECT preferred_copy_id FROM performances WHERE id = %s", (performance_id,)
        ).fetchone()
        return None if row is None else _uuid(row["preferred_copy_id"])

    def stored_lines(self, performance_id: UUID) -> list[StoredLine]:
        rows = self._conn.execute(
            """SELECT m.id, f.work_id
               FROM matches m LEFT JOIN library_files f ON f.id = m.library_file_id
               WHERE m.performance_id = %s ORDER BY m.id""",
            (performance_id,),
        ).fetchall()
        return [StoredLine(match_id=UUID(str(r["id"])), line_work_id=r["work_id"]) for r in rows]

    def files_waiting(self, after: UUID | None, limit: int) -> list[UUID]:
        rows = self._conn.execute(
            """SELECT id FROM library_files
               WHERE work_id IS NOT NULL AND performance_id IS NULL
                 AND (%s::uuid IS NULL OR id > %s::uuid)
               ORDER BY id LIMIT %s""",
            (after, after, limit),
        ).fetchall()
        return [UUID(str(r["id"])) for r in rows]

    def stranded_preferred_copies(self) -> list[UUID]:
        rows = self._conn.execute(
            """SELECT p.id FROM performances p
               JOIN library_files f ON f.id = p.preferred_copy_id
               WHERE f.file_status = 'missing' ORDER BY p.id"""
        ).fetchall()
        return [UUID(str(r["id"])) for r in rows]

    # --- writes --------------------------------------------------------------------------------

    def create(self, work_id: str, key: PerformanceKey, credits: list[NewCredit]) -> UUID:
        row = self._conn.execute(
            """INSERT INTO performances (work_id, recording_mbid, version_type, length_ms)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (work_id, key.recording_mbid, key.version_type.value, key.length_ms),
        ).fetchone()
        assert row is not None
        performance_id = UUID(str(row["id"]))
        for credit in credits:
            self._conn.execute(
                """INSERT INTO performance_credits
                       (performance_id, position, artist_id, mbid, credited_name, join_after,
                        role)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    performance_id,
                    credit.position,
                    credit.artist_id,
                    credit.mbid,
                    credit.credited_name,
                    credit.join_after,
                    credit.role.value,
                ),
            )
        return performance_id

    def hold(self, file_id: UUID, performance_id: UUID | None) -> None:
        self._conn.execute(
            "UPDATE library_files SET performance_id = %s WHERE id = %s",
            (performance_id, file_id),
        )

    def set_preferred_copy(self, performance_id: UUID, file_id: UUID | None) -> None:
        self._conn.execute(
            "UPDATE performances SET preferred_copy_id = %s WHERE id = %s",
            (file_id, performance_id),
        )

    def delete(self, performance_id: UUID) -> None:
        self._conn.execute("DELETE FROM performances WHERE id = %s", (performance_id,))
