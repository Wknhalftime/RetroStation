"""The sync-connection dependencies' teardown when the request's connection is already lost.

Requirements: G1 final review (I1 for ``get_sync_repos``, and the same pattern in
``get_mb_client``): a route that fails, e.g. with a domain error its router maps to 503, must
keep its own answer. Rolling back a dead connection raises ``OperationalError``, which would
replace that answer with a 500, so the teardown skips the rollback once the connection is
closed. The backend is terminated with ``pg_terminate_backend`` from a second connection.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterator
from typing import Any

import psycopg
import pytest

import backend.db.sync_conn as sync_conn
from backend.config import get_settings
from backend.dependencies import get_mb_client, get_sync_repos

pytestmark = pytest.mark.integration


class _RouteFailedError(Exception):
    """What the route raised: the teardown must let it through unchanged."""


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


def _fail_on_a_dead_connection(
    dependency: Generator[object], conns: list[psycopg.Connection[Any]], dsn: str
) -> None:
    next(dependency)
    (conn,) = conns
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))
    with pytest.raises(psycopg.OperationalError):
        conn.execute("SELECT 1")  # the route's own read meets the dead connection
    with pytest.raises(_RouteFailedError):
        dependency.throw(_RouteFailedError())


def test_get_mb_client_keeps_the_route_error_on_a_lost_connection(
    opened: list[psycopg.Connection[Any]], migrated_db: str
) -> None:
    _fail_on_a_dead_connection(get_mb_client(), opened, migrated_db)


def test_get_sync_repos_keeps_the_route_error_on_a_lost_connection(
    opened: list[psycopg.Connection[Any]], migrated_db: str
) -> None:
    _fail_on_a_dead_connection(get_sync_repos(), opened, migrated_db)
