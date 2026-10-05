from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg import AsyncConnection
from pydantic import BaseModel

from backend.dependencies import SyncRepos, get_current_token, get_db_connection
from backend.domain.system import SettingsError, StorageUnavailableError
from backend.services.setting_rules import save_setting

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
# NOTE (known gap, AUD-R010): GET still issues raw async SQL directly instead
# of delegating to UserSettingRepository.  The repository layer uses a
# synchronous psycopg connection, while FastAPI's dependency stack provides an
# AsyncConnection, and there is no async repository layer yet (AUD-R010:
# deferred, not rejected).  Until one exists, the SQL here and in
# PgUserSettingRepository must be kept in sync manually (same SELECT shape,
# same column set).  PUT below validates and writes through ``save_setting``
# on the sync repository instead (D27; PG2).
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
    cur = await conn.execute("SELECT key, value FROM user_settings ORDER BY key")
    rows = await cur.fetchall()
    return {row["key"]: row["value"] for row in rows}


@router.put("/{key}", response_model=SettingEntry)
def put_setting(key: str, body: SettingValue, repos: SyncRepos, _token: Token) -> SettingEntry:
    """Validate and store a setting by key (UPSERT; D27, PG2).

    Args:
        key: The setting key to create or overwrite.
        body: Request body containing the new ``value``.
        repos: The request's repositories, on a sync connection.
        _token: Auth token.

    Returns:
        The stored :class:`SettingEntry` with the key and persisted value.
    """
    try:
        saved = save_setting(repos.user_settings, key, body.value)
    except SettingsError as refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[{"loc": ["body", "value"], "msg": str(refused), "type": "value_error"}],
        ) from refused
    except StorageUnavailableError as lost:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable") from lost
    return SettingEntry(key=saved.key, value=saved.value)
