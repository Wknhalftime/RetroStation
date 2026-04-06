from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg

from backend.domain.enums import LogCategory, LogLevel
from backend.domain.models import SystemLog
from backend.repositories.system_logs import SystemLogRepository


class PgSystemLogRepository(SystemLogRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _row_to_model(self, row: dict[str, Any]) -> SystemLog:
        details = row["details"]
        if isinstance(details, str):
            details = json.loads(details)
        return SystemLog(
            id=UUID(str(row["id"])),
            trace_id=row.get("trace_id"),
            category=LogCategory(row["category"]),
            level=LogLevel(row["level"]),
            message=row["message"],
            details=details,
            created_at=row["created_at"],
        )

    def create(self, log: SystemLog) -> None:
        self._conn.execute(
            """INSERT INTO system_logs (id, trace_id, category, level, message, details, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                str(log.id),
                log.trace_id,
                log.category.value,
                log.level.value,
                log.message,
                json.dumps(log.details) if log.details is not None else None,
                log.created_at,
            ),
        )

    def _build_where(
        self,
        level: LogLevel | None,
        category: LogCategory | None,
        trace_id: str | None,
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if level is not None:
            conditions.append("level = %s")
            params.append(level.value)
        if category is not None:
            conditions.append("category = %s")
            params.append(category.value)
        if trace_id is not None:
            conditions.append("trace_id = %s")
            params.append(trace_id)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return where, params

    def list(
        self,
        *,
        level: LogLevel | None = None,
        category: LogCategory | None = None,
        trace_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SystemLog]:
        where, params = self._build_where(level, category, trace_id)
        params.extend([limit, offset])
        rows = self._conn.execute(
            f"SELECT * FROM system_logs {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params,
        ).fetchall()
        return [self._row_to_model(r) for r in rows]

    def count(
        self,
        *,
        level: LogLevel | None = None,
        category: LogCategory | None = None,
        trace_id: str | None = None,
    ) -> int:
        where, params = self._build_where(level, category, trace_id)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM system_logs {where}",
            params,
        ).fetchone()
        if row is None:
            return 0
        return int(row["n"])

