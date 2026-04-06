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
