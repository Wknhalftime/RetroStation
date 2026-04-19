"""WebSocket endpoint for real-time progress broadcast.

Authenticates via ``?token=`` query parameter. The browser WebSocket API does
not support custom headers on the initial HTTP upgrade handshake, so the token
is passed as a query parameter instead of the ``X-Airwave-Token`` header used
by the REST API.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import structlog
from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect
from psycopg_pool import PoolTimeout, TooManyRequests

from backend.config import get_settings
from backend.db.pool import get_pool

logger = structlog.get_logger()

POLL_INTERVAL_SECONDS = 0.5
STALE_THRESHOLD_MINUTES = 10
TERMINAL_GRACE_SECONDS = 5


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

                # Fetch running tasks plus recently-terminal rows so the
                # client briefly sees completed/failed states for its
                # green "Done" / red "Failed" beat before dismiss.
                cur = await conn.execute(
                    """SELECT task_id, task_type, status, progress_data,
                              started_at, updated_at
                       FROM progress_tracking
                       WHERE status = 'running'
                          OR (status IN ('completed', 'failed')
                              AND completed_at > now()
                                               - (interval '1 second' * %s))
                       ORDER BY started_at DESC""",
                    (TERMINAL_GRACE_SECONDS,),
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
    except (PoolTimeout, TooManyRequests) as exc:
        # DB pool saturated; HTTP routes return 503, WS analogue is 1013.
        logger.warning(
            "websocket_pool_saturated",
            exception=type(exc).__name__,
        )
        await websocket.close(code=1013, reason="Database pool saturated")
