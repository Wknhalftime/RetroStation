from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import get_current_token, get_db_connection

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SystemLogEntry(BaseModel):
    id: str
    trace_id: str | None
    category: str
    level: str
    message: str
    details: dict[str, Any] | None
    created_at: datetime


class SystemLogPage(BaseModel):
    total: int
    items: list[SystemLogEntry]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_entry(row: dict[str, Any]) -> SystemLogEntry:
    details = row["details"]
    if isinstance(details, str):
        details = json.loads(details)
    return SystemLogEntry(
        id=str(row["id"]),
        trace_id=row.get("trace_id"),
        category=row["category"],
        level=row["level"],
        message=row["message"],
        details=details,
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=SystemLogPage)
async def list_system_logs(
    conn: DbConn,
    _token: Token,
    level: Annotated[str | None, Query()] = None,
    category: Annotated[str | None, Query()] = None,
    trace_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SystemLogPage:
    """Return a paginated list of system log entries with optional filters."""
    conditions: list[str] = []
    params: list[Any] = []

    if level is not None:
        conditions.append("level = %s")
        params.append(level)
    if category is not None:
        conditions.append("category = %s")
        params.append(category)
    if trace_id is not None:
        conditions.append("trace_id = %s")
        params.append(trace_id)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    count_cur = await conn.execute(
        f"SELECT COUNT(*) AS n FROM system_logs {where}",
        params,
    )
    count_row = await count_cur.fetchone()
    total = int(count_row["n"]) if count_row else 0

    list_params = params + [limit, offset]
    cur = await conn.execute(
        f"SELECT * FROM system_logs {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
        list_params,
    )
    rows = await cur.fetchall()

    return SystemLogPage(total=total, items=[_parse_entry(r) for r in rows])


@router.get("/by-trace/{trace_id}", response_model=list[SystemLogEntry])
async def get_logs_by_trace(
    trace_id: str,
    conn: DbConn,
    _token: Token,
) -> list[SystemLogEntry]:
    """Return all system log entries for a given trace_id."""
    cur = await conn.execute(
        "SELECT * FROM system_logs WHERE trace_id = %s ORDER BY created_at ASC",
        (trace_id,),
    )
    rows = await cur.fetchall()
    return [_parse_entry(r) for r in rows]

