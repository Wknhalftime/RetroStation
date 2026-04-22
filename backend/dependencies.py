from collections.abc import AsyncGenerator, Generator
from typing import Annotated, Any

from fastapi import Header, HTTPException, status
from psycopg import AsyncConnection

from backend.config import get_settings
from backend.db.pool import get_pool
from backend.services.mb_client import MusicBrainzApiClient, MusicBrainzClientProtocol


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


async def get_db_connection() -> AsyncGenerator[AsyncConnection[Any]]:
    pool = get_pool()
    async with pool.connection() as conn:
        yield conn
        await conn.commit()


def get_mb_client() -> Generator[MusicBrainzClientProtocol]:
    """Yield a MusicBrainzApiClient backed by a fresh sync connection.

    Opens a dedicated sync psycopg connection (same pattern as Huey tasks)
    so the sync MB client and sync cache repository can share it.  On
    successful request handling the connection is committed; on exception
    the connection is rolled back so partial cache writes from a failed
    `/mb-artists` request never persist.
    """
    from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
    from backend.db.sync_conn import connect_sync

    settings = get_settings()
    with (
        connect_sync(settings.database_url) as conn,
        MusicBrainzApiClient(PgMusicBrainzCacheRepository(conn)) as client,
    ):
        try:
            yield client
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
