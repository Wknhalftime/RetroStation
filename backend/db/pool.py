from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None


async def _configure_search_path(conn: AsyncConnection[Any]) -> None:
    await conn.execute("SET search_path TO public, pg_catalog")
    # Pool requires configure to leave the connection idle (not INTRANS).
    await conn.commit()


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("Connection pool not initialized. Call init_pool() first.")
    return _pool


def init_pool(database_url: str) -> AsyncConnectionPool:
    global _pool
    _pool = AsyncConnectionPool(
        conninfo=database_url,
        min_size=2,
        max_size=10,
        # Bound the wait queue. psycopg_pool defaults to max_waiting=0
        # (unlimited), which turns sustained overload into unbounded queue
        # growth. 100 caps the queue at 10x max_size; each waiter is a
        # coroutine with ~1-2 KB of overhead, so the cap costs <200 KB
        # worst-case memory while still absorbing realistic bursts. The
        # expected load profile is single-user self-hosted (CORS restricts
        # origin to localhost:5173) with at most ~10-20 parallel fetches
        # per page load. Huey workers do NOT share this pool (they use
        # backend.db.sync_conn); consumers are the FastAPI HTTP routes
        # and the /ws WebSocket endpoint in backend.websocket. Overflow
        # raises psycopg_pool.TooManyRequests; the 30s `timeout` default
        # raises psycopg_pool.PoolTimeout. HTTP handlers registered in
        # backend.main translate both into 503 + Retry-After: 1; the
        # websocket endpoint translates them into WS close code 1013
        # ("Try Again Later") so browser clients can back off.
        max_waiting=100,
        open=False,  # opened explicitly in lifespan
        kwargs={"row_factory": dict_row},
        configure=_configure_search_path,
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
