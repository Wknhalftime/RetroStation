"""Fixtures for the phase 0b gate tests (SPEC 2026-10-28, gate tools): a bench database of the
tests' own, the app on it, the task queue in memory and the tools' process environment.

The gate tools refuse any database without "bench" in its name (S10), so these tests run on
``<test database>_bench``: created and migrated once per session (per xdist worker), emptied
before each test. None of these fixtures is autouse, so the other script tests in this folder
are untouched.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient
from huey.storage import MemoryStorage
from psycopg.rows import DictRow, dict_row

from backend.config import get_settings
from tests.conftest import _apply_test_db_tuning, _create_test_database
from tests.scripts.gate_world import app_client, bench_name, empty_database

type Conn = psycopg.Connection[DictRow]

# psycopg's async pool needs the selector event loop on Windows (as tests/routers/conftest.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# The environment the tools set for their own process (S10), restored after each test.
_TOOL_ENV = (
    "DATABASE_URL",
    "STREAM_ENABLED",
    "PYTEST_XDIST_WORKER",
    "RETROSTATION_SKIP_BOOT_MIGRATIONS",
)


@pytest.fixture(scope="session")
def bench_dsn(db_url: str) -> str:
    """``<test database>_bench``, created when missing (in the test tablespace when it exists),
    given the test databases' tuning and safety settings, and migrated from this tree. The name
    starts with the test prefix, so the root conftest's guards accept it, and holds "bench", so
    the gate tools accept it. Like the other test databases it is left for the end of a PR."""
    from backend.db.migrations import run_migrations

    params = psycopg.conninfo.conninfo_to_dict(db_url)
    name = bench_name(str(params["dbname"]))
    dsn = psycopg.conninfo.make_conninfo(**{**params, "dbname": name})
    admin = psycopg.conninfo.make_conninfo(**{**params, "dbname": "postgres"})
    with psycopg.connect(admin, autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is None:
            _create_test_database(conn, name)
        _apply_test_db_tuning(conn, name)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    with psycopg.connect(dsn) as conn:
        run_migrations(conn)
        conn.commit()
    return dsn


@pytest.fixture
def bench(bench_dsn: str) -> str:
    """The bench database, emptied: every table but ``schema_migrations``, and no copy marker."""
    empty_database(bench_dsn)
    return bench_dsn


@pytest.fixture
def conn(bench: str) -> Iterator[Conn]:
    with psycopg.connect(bench, row_factory=dict_row) as connection:
        yield connection


@pytest.fixture
def process_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The variables a tool sets for its own process (S10), put back after the test, and the
    settings cache cleared on both sides, so nothing a tool set leaks into later tests."""
    for key in _TOOL_ENV:
        if key in os.environ:
            monkeypatch.setenv(key, os.environ[key])
        else:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def tool_env(monkeypatch: pytest.MonkeyPatch, process_env: None, bench: str) -> None:
    """process_env, with the task entry points' settings on the bench database while a test
    seeds."""
    monkeypatch.setenv("DATABASE_URL", bench)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def queue_in_memory() -> Iterator[MemoryStorage]:
    """The hand-offs a seed or a tool queues land in memory, never in a worker's queue file
    (from the main checkout, a -n 0 run's queue file is the dev worker's)."""
    from backend.tasks.huey_app import huey

    storage = MemoryStorage(huey.name)
    patch = pytest.MonkeyPatch()
    patch.setattr(huey, "storage", storage)
    yield storage
    patch.undo()


@pytest.fixture
def no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production client's 1.1 s wait between calls, skipped while a seed fills the cache
    through a MockTransport."""
    from backend.services import mb_client

    monkeypatch.setattr(mb_client, "_rate_limit", lambda: None)


@pytest.fixture
def client(bench: str, tool_env: None) -> Iterator[TestClient]:
    """The app on the bench database for one test, its lifespan run with no boot migrations
    and streaming off; only the token is bypassed. Function-scoped, so it has stopped before a
    tool that boots the app itself runs in the same process."""
    with app_client(bench) as test_client:
        yield test_client
