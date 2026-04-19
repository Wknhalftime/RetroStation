from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.config import get_settings
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.sync_conn import connect_sync
from backend.dependencies import get_current_token, get_db_connection
from backend.tasks.library_enrichment_tasks import (
    library_enrichment_task as _library_enrichment_task,
)

router = APIRouter()

DbConn = Annotated[AsyncConnection[Any], Depends(get_db_connection)]
Token = Annotated[str, Depends(get_current_token)]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RetryEnrichmentResult(BaseModel):
    """Result returned by the retry-enrichment endpoint."""

    reset: int
    message: str


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
    """Return active and recently-terminal tasks, ordered by start time desc.

    Returns rows where ``status = 'running'``, plus ``failed`` or
    ``completed`` rows updated within the last 30 seconds so the frontend
    can display terminal states briefly.  The ``progress_data`` column is
    stored as JSONB but may arrive as a string in some driver
    configurations; this handler normalises both cases.

    Args:
        conn: Async database connection.
        _token: Auth token (validated by dependency).

    Returns:
        List of :class:`TaskInfo` for every running task, newest first.
    """
    cur = await conn.execute(
        """SELECT * FROM progress_tracking
           WHERE status = 'running'
              OR (status IN ('failed', 'completed')
                  AND updated_at > NOW() - INTERVAL '30 seconds')
           ORDER BY started_at DESC"""
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


@router.post("/retry-enrichment", response_model=RetryEnrichmentResult)
async def retry_enrichment(_token: Token) -> RetryEnrichmentResult:
    """Reset all enrichment_failed library files to pending and re-enqueue enrichment.

    Resets every ``library_files`` row whose ``enrichment_status`` is ``'failed'``
    back to ``'pending'``, then enqueues a fresh ``library_enrichment_task`` run.
    Use this when enrichment has stalled due to transient MusicBrainz errors or a
    task-level crash.

    Args:
        _token: Bearer token (auth check only).

    Returns:
        :class:`RetryEnrichmentResult` with the count of rows reset and a status
        message.
    """
    settings = get_settings()

    def _reset_failed() -> int:
        with connect_sync(settings.database_url) as conn:
            repo = PgLibraryFileRepository(conn)
            count = repo.reset_failed_enrichments()
            conn.commit()
            return count

    # asyncio.to_thread avoids blocking the event loop during the sync DB call.
    # Note: this does not fix thread-pool exhaustion under load — async repos
    # remain the long-term fix (see async infrastructure plan).
    reset_count = await asyncio.to_thread(_reset_failed)
    _library_enrichment_task()

    return RetryEnrichmentResult(
        reset=reset_count,
        message=(
            f"Reset {reset_count} failed file(s) to pending and queued enrichment."
        ),
    )
