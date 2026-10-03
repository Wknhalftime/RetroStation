"""``GET /matching/mb-artists`` when the database connection dies mid-request.

Requirements: G1 final review (a lost connection in a sync Pg repository answered 500; the
repository raises ``StorageUnavailableError`` and the router maps it to 503, as the sign-off
routes do for ``ClipStorageError``) and ``get_mb_client``'s teardown, which must not replace
that 503 with a 500 by rolling back the dead connection. The dependencies are the real ones on
the real PostgreSQL test database; only the token is bypassed. The request's backend is
terminated with ``pg_terminate_backend`` from a second connection right after it connects, so
the search's cache read meets the dead connection before any call to MusicBrainz.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import FastAPI

import backend.db.sync_conn as sync_conn
from backend.config import get_settings
from backend.dependencies import get_current_token
from backend.routers.v1 import router as v1_router

pytestmark = pytest.mark.integration

MB_ARTISTS = "/api/v1/matching/mb-artists"


@pytest.fixture
def app(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    get_settings.cache_clear()
    built = FastAPI()
    built.include_router(v1_router)
    built.dependency_overrides[get_current_token] = lambda: "test-token"
    try:
        yield built
    finally:
        get_settings.cache_clear()


@pytest.fixture
def connections_die(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every sync connection has its backend terminated as soon as it opens; returns the pids."""
    real: Callable[..., psycopg.Connection[Any]] = sync_conn.connect_sync
    killed: list[int] = []

    def connect(dsn: str, **kwargs: Any) -> psycopg.Connection[Any]:
        conn = real(dsn, **kwargs)
        with psycopg.connect(migrated_db, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))
        killed.append(conn.info.backend_pid)
        return conn

    monkeypatch.setattr(sync_conn, "connect_sync", connect)
    return killed


async def test_a_connection_lost_in_the_search_is_503(
    app: FastAPI, connections_die: list[int]
) -> None:
    # raise_app_exceptions: an error in the dependency's teardown fails the test outright.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get(MB_ARTISTS, params={"query": "Blondie"})
    assert len(connections_die) == 1
    assert response.status_code == 503
    assert response.json() == {"detail": "unavailable"}
