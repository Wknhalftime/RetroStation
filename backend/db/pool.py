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
        # (unlimited) which turns a saturated pool into unbounded memory
        # growth under sustained overload. 50 caps the queue at 5x max_size;
        # additional requests fail fast with PoolTimeout instead of being
        # queued indefinitely, surfacing overload as 503s rather than OOM.
        max_waiting=50,
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
