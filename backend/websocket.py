"""WebSocket endpoint for real-time progress broadcast."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast

from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect

from backend.config import get_settings
from backend.db.pool import get_pool

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 0.5
STALE_THRESHOLD_MINUTES = 10


async def websocket_endpoint(websocket: WebSocket) -> None:
    """Handle a WebSocket connection for task progress broadcasting.

    Authenticates via ``?token=`` query parameter, then polls the
    ``progress_tracking`` table every 500 ms and broadcasts running task
    info as JSON.  Tasks not updated within 10 minutes are marked failed
    before each broadcast.

    Args:
        websocket: The incoming WebSocket connection.
    """
    token = websocket.query_params.get("token")
    settings = get_settings()

    if token != settings.airwave_token:
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()
    logger.info("WebSocket client connected")

    pool = get_pool()

    try:
        while True:
            async with pool.connection() as conn:
                # Mark stale tasks as failed
                await conn.execute(
                    """UPDATE progress_tracking
                       SET status = 'failed'
                       WHERE status = 'running'
                         AND updated_at < now() - (interval '1 minute' * %s)""",
                    (STALE_THRESHOLD_MINUTES,),
                )
                await conn.commit()

                # Fetch all currently running tasks
                cur = await conn.execute(
                    """SELECT task_id, task_type, status, progress_data,
                              started_at, updated_at
                       FROM progress_tracking
                       WHERE status = 'running'
                       ORDER BY started_at DESC"""
                )
                rows = cast(list[dict[str, Any]], await cur.fetchall())

            tasks: list[dict[str, Any]] = []
            for row in rows:
                progress_data = row["progress_data"]
                if isinstance(progress_data, str):
                    progress_data = json.loads(progress_data)

                tasks.append(
                    {
                        "task_id": row["task_id"],
                        "task_type": row["task_type"],
                        "status": row["status"],
                        "progress_data": progress_data,
                        "started_at": row["started_at"].isoformat(),
                        "updated_at": row["updated_at"].isoformat(),
                    }
                )

            await websocket.send_json({"tasks": tasks})
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
