from __future__ import annotations

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


class SettingValue(BaseModel):
    """Request body for PUT /settings/{key}."""

    value: str


class SettingEntry(BaseModel):
    """Response body for a single setting."""

    key: str
    value: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=dict[str, str])
async def get_all_settings(conn: DbConn, _token: Token) -> dict[str, str]:
    """Return all user settings as a key/value mapping ordered by key.

    Args:
        conn: Async database connection.
        _token: Auth token (validated by dependency).

    Returns:
        Dictionary mapping each setting key to its value.
    """
    cur = await conn.execute(
        "SELECT key, value FROM user_settings ORDER BY key"
    )
    rows = await cur.fetchall()
    return {row["key"]: row["value"] for row in rows}


@router.put("/{key}", response_model=SettingEntry)
async def put_setting(
    key: str, body: SettingValue, conn: DbConn, _token: Token
) -> SettingEntry:
    """Create or update a setting by key (UPSERT).

    Args:
        key: The setting key to create or overwrite.
        body: Request body containing the new ``value``.
        conn: Async database connection.
        _token: Auth token.

    Returns:
        The stored :class:`SettingEntry` with the key and persisted value.
    """
    await conn.execute(
        """
        INSERT INTO user_settings (key, value, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET
            value      = EXCLUDED.value,
            updated_at = now()
        """,
        (key, body.value),
    )
    return SettingEntry(key=key, value=body.value)
