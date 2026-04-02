from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import get_current_token, get_db_connection

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TaskInfo(BaseModel):
    """Summary of a single tracked background task."""

    task_id: str
    task_type: str
    status: str
    progress_data: dict[str, Any]
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/active", response_model=list[TaskInfo])
async def get_active_tasks(conn: DbConn, _token: Token) -> list[TaskInfo]:
    """Return all currently running tasks, ordered by start time descending.

    Only rows where ``status = 'running'`` are returned.  The ``progress_data``
    column is stored as JSONB but may arrive as a string in some driver
    configurations; this handler normalises both cases.

    Args:
        conn: Async database connection.
        _token: Auth token (validated by dependency).

    Returns:
        List of :class:`TaskInfo` for every running task, newest first.
    """
    cur = await conn.execute(
        "SELECT * FROM progress_tracking WHERE status = 'running' ORDER BY started_at DESC"
    )
    rows = await cur.fetchall()

    tasks: list[TaskInfo] = []
    for row in rows:
        progress_data = row["progress_data"]
        if isinstance(progress_data, str):
            progress_data = json.loads(progress_data)

        tasks.append(
            TaskInfo(
                task_id=row["task_id"],
                task_type=row["task_type"],
                status=row["status"],
                progress_data=progress_data if progress_data is not None else {},
                started_at=row["started_at"],
                updated_at=row["updated_at"],
                completed_at=row.get("completed_at"),
            )
        )

    return tasks
