from __future__ import annotations

import os
from collections.abc import Generator
from typing import Any

import psycopg
import pytest
from psycopg import sql as pg_sql

_TRUNCATE_SQL = """
    TRUNCATE play_events, track_identities, broadcast_artists,
             playlists, broadcast_days, stations,
             matches, mapping_rules,
             artists, works, recordings,
             library_files, library_quarantine,
             song_masters, format_overrides,
             mb_cache, progress_tracking, user_settings,
             system_logs,
             library_folder_staged_hashes, library_folders
    CASCADE
"""

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://retrostation:retrostation-dev@localhost:5432/retrostation_test",
)


@pytest.fixture(scope="session")
def db_url(worker_id: str) -> str:
    """Return a worker-specific DB URL to isolate parallel test workers.

    master  → retrostation_test          (sequential run or CI without -n)
    gw0     → retrostation_test_gw0
    gw1     → retrostation_test_gw1
    …
    """
    if worker_id == "master":
        return TEST_DATABASE_URL
    params = psycopg.conninfo.conninfo_to_dict(TEST_DATABASE_URL)
    params["dbname"] = f"{params['dbname']}_{worker_id}"
    return psycopg.conninfo.make_conninfo(**params)


@pytest.fixture(scope="session")
def clean_db(db_url: str, worker_id: str) -> None:
    """Ensure the worker DB exists, then drop and recreate its public schema."""
    if worker_id != "master":
        params = psycopg.conninfo.conninfo_to_dict(db_url)
        dbname = params["dbname"]
        admin_params = dict(params)
        admin_params["dbname"] = "postgres"
        admin_url = psycopg.conninfo.make_conninfo(**admin_params)
        with psycopg.connect(admin_url, autocommit=True) as admin_conn:
            exists = admin_conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
            ).fetchone()
            if not exists:
                admin_conn.execute(
                    pg_sql.SQL("CREATE DATABASE {}").format(pg_sql.Identifier(dbname))
                )

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture(scope="session")
def _migrated_db_url(clean_db: None, db_url: str) -> str:
    from backend.db.migrations import run_migrations
    with psycopg.connect(db_url) as conn:
        run_migrations(conn)
        conn.commit()
    return db_url


@pytest.fixture(scope="session")
def _db_session_conn(
    _migrated_db_url: str,
) -> Generator[psycopg.Connection[Any]]:
    """Session-scope autocommit connection reused across all migrated_db calls.

    Each xdist worker owns its own DB (db_url fixture), so one connection per
    worker. Reusing amortizes ~45 ms psycopg handshake across ~120 tests.
    Autocommit means no wrapping transaction — safe to share across tests for
    TRUNCATE statements. Do NOT open transactions on this connection.
    """
    conn = psycopg.connect(_migrated_db_url, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def migrated_db(
    _migrated_db_url: str,
    _db_session_conn: psycopg.Connection[Any],
) -> str:
    """Per-test fixture: truncates all tables then returns the DB URL."""
    try:
        _db_session_conn.execute(_TRUNCATE_SQL)
    except psycopg.OperationalError:
        # Session conn dropped (idle timeout etc.); fall back to a fresh conn.
        with psycopg.connect(_migrated_db_url, autocommit=True) as conn:
            conn.execute(_TRUNCATE_SQL)
    return _migrated_db_url
