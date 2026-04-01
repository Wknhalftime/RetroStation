from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import HTTPException, Header, status
from psycopg import AsyncConnection

from backend.config import get_settings
from backend.db.pool import get_pool


async def get_current_token(
    x_airwave_token: Annotated[str | None, Header()] = None,
) -> str:
    settings = get_settings()
    if x_airwave_token != settings.airwave_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Airwave-Token header",
        )
    return x_airwave_token


async def get_db_connection() -> AsyncGenerator[AsyncConnection[Any], None]:
    pool = get_pool()
    async with pool.connection() as conn:
        yield conn
