from __future__ import annotations

import asyncio
import os
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
    """Session-scoped TestClient. Pool created once, reused across tests."""
    os.environ["DATABASE_URL"] = _migrated_db_url

    from backend.config import get_settings

    get_settings.cache_clear()

    from backend.dependencies import get_current_token
    from backend.main import app

    async def _skip_auth() -> str:
        return "test-token"

    app.dependency_overrides[get_current_token] = _skip_auth

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def client(_router_client: TestClient, migrated_db: str) -> TestClient:
    """Per-test client: tables are truncated before each test."""
    return _router_client


@pytest.fixture
def db_conn(migrated_db: str) -> Generator[psycopg.Connection[dict]]:
    """Sync connection for inserting test data."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield conn
