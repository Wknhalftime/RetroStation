"""The sync-connection dependencies' commit when the connection is lost after the route has
succeeded (PR #133 item 3, confirmed with the G1 coordinator).

Requirements: G1 final review (a lost connection answered 500). ``get_sync_repos`` commits
before the response is sent (its "function" scope), so a commit that meets a dead connection
means nothing was stored: the client gets ``503 unavailable``, never the route's success nor a
500. ``get_mb_client``'s commit only keeps the MusicBrainz cache rows the search wrote; the
search's answer is already right, so a lost cache commit is logged and the answer stands.
The backend is terminated with ``pg_terminate_backend`` from a second connection.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from structlog.testing import capture_logs

import backend.db.sync_conn as sync_conn
from backend.config import get_settings
from backend.dependencies import SyncRepos, get_mb_client
from backend.domain.system import UserSetting

pytestmark = pytest.mark.integration


def _terminate(dsn: str, conn: psycopg.Connection[Any]) -> None:
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))


@pytest.fixture
def opened(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[psycopg.Connection[Any]]]:
    """The connections the dependencies open, on the test database."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    get_settings.cache_clear()
    real: Callable[..., psycopg.Connection[Any]] = sync_conn.connect_sync
    conns: list[psycopg.Connection[Any]] = []

    def connect(dsn: str, **kwargs: Any) -> psycopg.Connection[Any]:
        conn = real(dsn, **kwargs)
        conns.append(conn)
        return conn

    monkeypatch.setattr(sync_conn, "connect_sync", connect)
    try:
        yield conns
    finally:
        get_settings.cache_clear()


async def test_a_sync_repos_commit_on_a_lost_connection_is_503(
    opened: list[psycopg.Connection[Any]], migrated_db: str
) -> None:
    app = FastAPI()

    @app.put("/probe")
    def probe(repos: SyncRepos) -> dict[str, bool]:
        repos.user_settings.upsert(UserSetting(key="lost.commit", value="1"))
        _terminate(migrated_db, opened[0])  # the server goes away before the commit
        return {"stored": True}

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.put("/probe")
    assert response.status_code == 503
    assert response.json() == {"detail": "unavailable"}


def test_a_lost_mb_cache_commit_is_logged_and_the_answer_stands(
    opened: list[psycopg.Connection[Any]], migrated_db: str
) -> None:
    dependency = get_mb_client()
    next(dependency)
    opened[0].execute("SELECT 1")  # the search's cache writes opened a transaction
    _terminate(migrated_db, opened[0])
    with capture_logs() as events, pytest.raises(StopIteration):
        next(dependency)  # the route returned: the teardown commits
    lost = [e for e in events if e.get("event") == "mb_cache_commit_lost"]
    assert len(lost) == 1
