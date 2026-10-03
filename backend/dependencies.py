from collections.abc import AsyncGenerator, Generator
from ipaddress import IPv4Address
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status
from psycopg import AsyncConnection

from backend.config import BindHost, get_settings, is_internal_client
from backend.db.pool import get_pool
from backend.services.mb_client import MusicBrainzApiClient, MusicBrainzClientProtocol
from backend.services.missing_file_reconciliation_service import ReconciliationRepos
from backend.services.repository_factory import RepositoryFactory, reconciliation_repos
from backend.services.streaming.service import StreamService
from backend.services.streaming.station_years import StationYearRepos

_LOOPBACK_BIND: BindHost = IPv4Address("127.0.0.1")


def require_internal_client(request: Request) -> None:
    """403 unless the request comes from this machine, as the API is bound (D24, D45)."""
    client = "" if request.client is None else request.client.host
    bind: BindHost = getattr(request.app.state, "server_host", _LOOPBACK_BIND)
    if not is_internal_client(client, bind):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="internal clients only")


def get_stream_service(request: Request) -> StreamService:
    """The app's stream service; 503 ``unavailable`` while streaming is off (D34)."""
    service: StreamService | None = getattr(request.app.state, "stream_service", None)
    if service is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="unavailable")
    return service


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

    A connection already lost is not rolled back, as in ``get_sync_repos``: the rollback
    would raise and replace the route's own answer (a 503 for ``StorageUnavailableError``)
    with a 500, and a dead connection has nothing left to roll back.
    """
    from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
    from backend.db.sync_conn import connect_sync

    settings = get_settings()
    with (
        connect_sync(settings.database_url) as conn,
        MusicBrainzApiClient(
            PgMusicBrainzCacheRepository(conn),
            ttl_days=settings.mb_cache_ttl_days,
        ) as client,
    ):
        try:
            yield client
        except BaseException:
            if not conn.closed:
                conn.rollback()
            raise
        else:
            conn.commit()


def get_sync_repos() -> Generator[RepositoryFactory]:
    """Pg repositories on a fresh sync connection, for sync endpoints calling sync services.

    Committed when the request succeeds, rolled back when it raises (an
    HTTPException mapped from a domain error included). Depend on it through
    ``SyncRepos``: its "function" scope ends the transaction before the response
    is sent, and one shared scope keeps it to one connection per request.

    A connection already lost (e.g. its commit failed because the server went away) is not
    rolled back (I1): that rollback would raise and replace the route's own answer, such as
    a 503 for the failed commit, with a 500. psycopg's ``__exit__`` then skips it too.
    """
    from backend.db.sync_conn import connect_sync

    with connect_sync(get_settings().database_url) as conn:
        try:
            yield RepositoryFactory(conn)
        except BaseException:
            if not conn.closed:
                conn.rollback()
            raise
        else:
            conn.commit()


# "function": commit before the response goes out, so a client never sees a success
# whose commit then fails, nor refetches ahead of it. FastAPI caches a dependency per
# (function, scope), so every user must share this one alias to share the connection.
SyncRepos = Annotated[RepositoryFactory, Depends(get_sync_repos, scope="function")]


def get_reconciliation_repos(repos: SyncRepos) -> ReconciliationRepos:
    """The fold repositories for one request, on the request's connection."""
    return reconciliation_repos(repos)


def get_station_year_repos(repos: SyncRepos) -> StationYearRepos:
    """The repositories the station-year list reads, on the request's connection (D70).

    No dependency on ``stream_service``: the list works while streaming is off.
    """
    return repos.station_year_repos()
