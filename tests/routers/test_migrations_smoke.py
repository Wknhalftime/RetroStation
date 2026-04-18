"""Cold-path smoke test: verify the real FastAPI lifespan migration path still runs.

This test deliberately does NOT set RETROSTATION_SKIP_BOOT_MIGRATIONS=1, so
`backend.main.lifespan` executes `run_migrations(conn)` for real. It exists to
catch lifespan-boot regressions that would otherwise be invisible because every
other router test skips the migration step.

Marked `slow` so it runs in the nightly lane, not the PR CI fast path.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

import backend.main as backend_main
from backend.db.migrations import run_migrations as _real_run_migrations

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.mark.slow
def test_lifespan_runs_migrations_when_flag_unset(
    _migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lifespan boots successfully AND actually invokes run_migrations once.

    A bare /health assertion would pass even if lifespan stopped calling
    run_migrations (the DB is already migrated by the session fixture). The
    spy delegates to the real migration runner so the underlying path is
    exercised and the observable call count is verified.
    """
    monkeypatch.setenv("DATABASE_URL", _migrated_db_url)
    monkeypatch.delenv("RETROSTATION_SKIP_BOOT_MIGRATIONS", raising=False)

    calls: list[psycopg.Connection[Any]] = []

    def _spy_run_migrations(conn: psycopg.Connection[Any]) -> None:
        calls.append(conn)
        _real_run_migrations(conn)

    monkeypatch.setattr(backend_main, "run_migrations", _spy_run_migrations)

    from backend.config import get_settings

    get_settings.cache_clear()

    from backend.dependencies import get_current_token

    async def _skip_auth() -> str:
        return "test-token"

    backend_main.app.dependency_overrides[get_current_token] = _skip_auth

    try:
        with TestClient(backend_main.app, raise_server_exceptions=False) as c:
            response = c.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
    finally:
        backend_main.app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert len(calls) == 1, (
        f"lifespan should invoke run_migrations exactly once when flag is "
        f"unset, got {len(calls)}"
    )
