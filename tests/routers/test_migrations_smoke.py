"""Cold-path smoke test: verify the real FastAPI lifespan migration path still runs.

This test deliberately does NOT set RETROSTATION_SKIP_BOOT_MIGRATIONS=1, so
`backend.main.lifespan` executes `run_migrations(conn)` for real. It exists to
catch lifespan-boot regressions that would otherwise be invisible because every
other router test skips the migration step.

Marked `slow` so it runs in the nightly lane, not the PR CI fast path.
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture
def _cold_path_client(_migrated_db_url: str) -> Generator[TestClient]:
    os.environ["DATABASE_URL"] = _migrated_db_url
    os.environ.pop("RETROSTATION_SKIP_BOOT_MIGRATIONS", None)

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


@pytest.mark.slow
def test_lifespan_runs_migrations_when_flag_unset(
    _cold_path_client: TestClient,
) -> None:
    """Lifespan boots successfully with real run_migrations call (idempotent)."""
    response = _cold_path_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
