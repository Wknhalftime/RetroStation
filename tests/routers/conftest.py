from __future__ import annotations

import asyncio
import sys
from collections.abc import Generator

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

# psycopg async requires SelectorEventLoop on Windows (not ProactorEventLoop)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(scope="module")
def _router_client(_migrated_db_url: str) -> Generator[TestClient]:
    """Module-scoped TestClient with auth bypass for router tests.

    Created once per test module and torn down after, clearing dependency
    overrides and settings cache to prevent state leakage across modules
    under pytest-xdist parallel execution.

    Env vars (DATABASE_URL, RETROSTATION_SKIP_BOOT_MIGRATIONS) are scoped via
    a module-local MonkeyPatch instance and restored on teardown so later test
    modules on the same xdist worker don't inherit them.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", _migrated_db_url)
    # Session fixture already migrated this DB URL; skip redundant lifespan migration.
    mp.setenv("RETROSTATION_SKIP_BOOT_MIGRATIONS", "1")

    from backend.config import get_settings

    get_settings.cache_clear()

    from backend.dependencies import get_current_token
    from backend.main import app

    async def _skip_auth() -> str:
        return "test-token"

    app.dependency_overrides[get_current_token] = _skip_auth

    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        mp.undo()


@pytest.fixture
def client(_router_client: TestClient, migrated_db: str) -> TestClient:
    """Per-test client: tables are truncated before each test."""
    return _router_client


@pytest.fixture
def db_conn(migrated_db: str) -> Generator[psycopg.Connection[dict]]:
    """Sync connection for inserting test data."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield conn
