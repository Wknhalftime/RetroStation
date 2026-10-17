"""WebSocket endpoint for real-time progress broadcast.

Authenticates via ``?token=`` query parameter. The browser WebSocket API does
not support custom headers on the initial HTTP upgrade handshake, so the token
is passed as a query parameter instead of the ``X-Airwave-Token`` header used
by the REST API.

One ``ProgressFeed`` polls ``progress_tracking`` for every connected client: each open tab
used to run its own reaper UPDATE, commit and table scan twice a second.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import psycopg
import structlog
from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect
from psycopg_pool import AsyncConnectionPool, PoolTimeout, TooManyRequests

from backend.config import get_settings

logger = structlog.get_logger()

POLL_INTERVAL_SECONDS = 0.5
STALE_THRESHOLD_MINUTES = 10
TERMINAL_GRACE_SECONDS = 5
STALE_SQL = """UPDATE progress_tracking
                       SET status = 'failed', completed_at = now()
                       WHERE status = 'running'
                         AND updated_at < now() - (interval '1 minute' * %s)"""
"""The ``/ws`` feed's reaper: marks a ``running`` row ``failed`` when it was not updated for
the given minutes (``STALE_THRESHOLD_MINUTES``). The cost meter's row stays fresh by being
rewritten every 2 s with a UTC-aware time (design note 11; D90, I5)."""

# Running tasks plus recently-terminal rows, so the client briefly sees completed/failed
# states for its green "Done" / red "Failed" beat before dismiss.
FEED_SQL = """SELECT task_id, task_type, status, progress_data,
                     started_at, updated_at, completed_at
              FROM progress_tracking
              WHERE status = 'running'
                 OR (status IN ('completed', 'failed')
                     AND coalesce(completed_at, updated_at) > now()
                                      - (interval '1 second' * %s))
              ORDER BY started_at DESC"""

type Frame = dict[str, list[dict[str, Any]]]


@dataclass(frozen=True)
class FeedClosed:
    """The poll failed; every client is closed with this WebSocket close code."""

    code: int
    reason: str


type FeedItem = Frame | FeedClosed


def _task_json(row: dict[str, Any]) -> dict[str, Any]:
    progress_data = row["progress_data"]
    if isinstance(progress_data, str):
        progress_data = json.loads(progress_data)
    completed_at = row.get("completed_at")
    return {
        "task_id": row["task_id"],
        "task_type": row["task_type"],
        "status": row["status"],
        "progress_data": progress_data,
        "started_at": row["started_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "completed_at": completed_at.isoformat() if completed_at else None,
    }


async def poll_progress(pool: AsyncConnectionPool) -> Frame:
    """Reap stale running rows, then read the rows the feed shows."""
    async with pool.connection() as conn:
        await conn.execute(STALE_SQL, (STALE_THRESHOLD_MINUTES,))
        await conn.commit()
        cur = await conn.execute(FEED_SQL, (TERMINAL_GRACE_SECONDS,))
        rows = cast(list[dict[str, Any]], await cur.fetchall())
    return {"tasks": [_task_json(row) for row in rows]}


def _failure(error: psycopg.Error | PoolTimeout | TooManyRequests) -> FeedClosed:
    if isinstance(error, PoolTimeout | TooManyRequests):
        # DB pool saturated; HTTP routes return 503, the WS analogue is 1013.
        logger.warning("websocket_pool_saturated", exception=type(error).__name__)
        return FeedClosed(1013, "Database pool saturated")
    logger.warning("websocket_poll_failed", exception=type(error).__name__, error=str(error))
    return FeedClosed(1011, "Database unavailable")


@dataclass
class _Subscriber:
    first_poll: int
    """The first poll this client may see: one that started after it subscribed, so its first
    frame never predates its connection."""
    items: asyncio.Queue[FeedItem]


class ProgressFeed:
    """Polls once per interval while at least one client listens, and hands each client the
    newest item; a client that falls behind skips frames instead of queueing them."""

    def __init__(self, poll: Callable[[], Awaitable[Frame]], interval_s: float) -> None:
        self._poll = poll
        self._interval_s = interval_s
        self._subscribers: list[_Subscriber] = []
        self._polls_started = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="progress_feed")
        self._task.add_done_callback(_report_crash)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.wait({self._task})
        self._task = None

    @asynccontextmanager
    async def subscription(self) -> AsyncIterator[asyncio.Queue[FeedItem]]:
        subscriber = _Subscriber(self._polls_started + 1, asyncio.Queue(maxsize=1))
        self._subscribers.append(subscriber)
        self._wake.set()  # a new client gets its first frame now, not after the interval
        try:
            yield subscriber.items
        finally:
            self._subscribers.remove(subscriber)

    async def _run(self) -> None:
        while True:
            if not self._subscribers:
                await self._wake.wait()
            self._wake.clear()
            self._polls_started += 1
            poll_number = self._polls_started
            item = await self._poll_once()
            for subscriber in self._subscribers:
                if poll_number >= subscriber.first_poll:
                    _offer(subscriber.items, item)
            await self._sleep_unless_woken()

    async def _poll_once(self) -> FeedItem:
        try:
            return await self._poll()
        except (psycopg.Error, PoolTimeout, TooManyRequests) as error:
            return _failure(error)

    async def _sleep_unless_woken(self) -> None:
        waiter = asyncio.create_task(self._wake.wait())
        await asyncio.wait({waiter}, timeout=self._interval_s)
        waiter.cancel()


def _report_crash(task: asyncio.Task[None]) -> None:
    """A feed that died of a bug leaves every client waiting; say so loudly."""
    if not task.cancelled() and task.exception() is not None:
        logger.error("progress_feed_crashed", exc_info=task.exception())


def _offer(items: asyncio.Queue[FeedItem], item: FeedItem) -> None:
    """Replace the client's unsent item, if any, with *item*."""
    if items.full():
        items.get_nowait()
    items.put_nowait(item)


async def websocket_endpoint(websocket: WebSocket, feed: ProgressFeed) -> None:
    """Handle a WebSocket connection for task progress broadcasting.

    Authenticates via ``?token=`` query parameter, then sends every frame the shared feed
    polls (every 500 ms): running tasks plus tasks that ended in the last 5 s. Tasks not
    updated within 10 minutes are marked failed before each poll.

    Args:
        websocket: The incoming WebSocket connection.
        feed: The app's progress feed (built in the lifespan).
    """
    token = websocket.query_params.get("token")
    settings = get_settings()

    if token != settings.airwave_token:
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()
    logger.info("WebSocket client connected")

    async with feed.subscription() as items:
        try:
            while True:
                item = await items.get()
                if isinstance(item, FeedClosed):
                    await websocket.close(code=item.code, reason=item.reason)
                    return
                await websocket.send_json(item)
        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
