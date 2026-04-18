from __future__ import annotations

import contextlib
import os
import warnings
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


_TEST_DB_SETTINGS: tuple[tuple[str, str], ...] = (
    # Performance: skip WAL fsync wait on commit. Safe because crash durability
    # doesn't matter for test DBs — the worker DB is rebuilt from scratch every
    # session. Never apply to production.
    ("synchronous_commit", "off"),
    # Safety: bound runaway queries. Top legitimate test query is ~4s (CSV E2E);
    # 15s leaves 3.75x headroom. Anything longer is a hang worth surfacing.
    ("statement_timeout", "15s"),
    # Safety: cap lock waits. TRUNCATE CASCADE is ~250 ms under no contention;
    # 5s means any real lock contention (e.g. a leaked ACCESS EXCLUSIVE from
    # another worker) surfaces as a test failure instead of a job-wide hang.
    ("lock_timeout", "5s"),
    # Safety: kill connections left idle-in-transaction by a test that forgot
    # to commit/rollback. Prevents ACCESS EXCLUSIVE / row locks from pinning
    # other workers. 30s is 6x the longest expected fixture setup.
    ("idle_in_transaction_session_timeout", "30s"),
)


def _apply_test_db_tuning(admin_conn: psycopg.Connection[Any], dbname: str) -> None:
    """Apply test-only, performance-and-safety settings to a test database.

    Scoped via `ALTER DATABASE`, inherited by every new connection to this DB
    (fixture conns, app pool, seed helpers). Takes effect only for connections
    opened AFTER this runs; clean_db runs first in the fixture chain, so all
    downstream conns inherit the settings.

    Hard safety gate: refuses unless the dbname begins with the test prefix.
    Rules out misconfiguration pointing tests at a prod URL.
    """
    if not dbname.startswith("retrostation_test"):
        raise RuntimeError(
            f"refusing to apply test tuning to non-test database {dbname!r}"
        )
    for setting, value in _TEST_DB_SETTINGS:
        admin_conn.execute(
            pg_sql.SQL("ALTER DATABASE {} SET {} = {}").format(
                pg_sql.Identifier(dbname),
                pg_sql.Identifier(setting),
                pg_sql.Literal(value),
            )
        )


@pytest.fixture(scope="session")
def clean_db(db_url: str, worker_id: str) -> None:
    """Ensure the worker DB exists, then drop and recreate its public schema.

    The admin connection to the ``postgres`` maintenance DB is required when
    ``worker_id != "master"`` (to CREATE DATABASE for the per-worker suffix)
    and optional for ``"master"`` (only used to ALTER DATABASE for tuning).
    If the test role lacks CONNECT privilege on ``postgres``, the master
    branch degrades gracefully: a warning is emitted and tuning is skipped,
    so the suite still runs against an untuned but functional test DB.
    """
    params = psycopg.conninfo.conninfo_to_dict(db_url)
    dbname = params["dbname"]
    admin_params = dict(params)
    admin_params["dbname"] = "postgres"
    admin_url = psycopg.conninfo.make_conninfo(**admin_params)

    try:
        admin_cm = psycopg.connect(admin_url, autocommit=True)
    except psycopg.OperationalError as exc:
        if worker_id != "master":
            # Per-worker DBs REQUIRE admin access; no safe fallback.
            raise
        warnings.warn(
            f"tests: could not connect to 'postgres' maintenance DB ({exc}); "
            "skipping per-database tuning (synchronous_commit/statement_timeout/"
            "lock_timeout/idle_in_transaction_session_timeout). The suite will "
            "still run but slower and without the runaway-query safety nets.",
            stacklevel=1,
        )
    else:
        with admin_cm as admin_conn:
            if worker_id != "master":
                exists = admin_conn.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
                ).fetchone()
                if not exists:
                    admin_conn.execute(
                        pg_sql.SQL("CREATE DATABASE {}").format(
                            pg_sql.Identifier(dbname)
                        )
                    )
            # Apply unsafe-but-fast settings scoped to this test DB only.
            # Takes effect for connections opened AFTER this runs (which is fine:
            # clean_db runs first in the fixture chain).
            _apply_test_db_tuning(admin_conn, dbname)

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


class _SessionConnHolder:
    """Mutable holder so migrated_db can swap in a fresh connection on failure.

    The raw Connection object cannot be mutated in place; yielding a holder lets
    the per-test fixture replace a broken session connection without leaving the
    old one cached for subsequent tests (which would re-incur the reconnect
    cost and mask the underlying fault).
    """

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url
        self.conn: psycopg.Connection[Any] = psycopg.connect(db_url, autocommit=True)

    def reconnect(self) -> None:
        # Broken conn may refuse close(); drop it regardless.
        with contextlib.suppress(psycopg.Error):
            self.conn.close()
        self.conn = psycopg.connect(self._db_url, autocommit=True)

    def close(self) -> None:
        with contextlib.suppress(psycopg.Error):
            self.conn.close()


@pytest.fixture(scope="session")
def _db_session_conn(
    _migrated_db_url: str,
) -> Generator[_SessionConnHolder]:
    """Session-scope autocommit connection reused across all migrated_db calls.

    Each xdist worker owns its own DB (db_url fixture), so one connection per
    worker. Reusing amortizes ~45 ms psycopg handshake across ~120 tests.
    Autocommit means no wrapping transaction — safe to share across tests for
    TRUNCATE statements. Do NOT open transactions on this connection.
    """
    holder = _SessionConnHolder(_migrated_db_url)
    try:
        yield holder
    finally:
        holder.close()


@pytest.fixture
def migrated_db(
    _migrated_db_url: str,
    _db_session_conn: _SessionConnHolder,
) -> str:
    """Per-test fixture: truncates all tables then returns the DB URL."""
    try:
        _db_session_conn.conn.execute(_TRUNCATE_SQL)
    except psycopg.OperationalError:
        # Session conn dropped (idle timeout etc.); invalidate and reconnect so
        # later tests don't keep paying the exception+reconnect path.
        _db_session_conn.reconnect()
        _db_session_conn.conn.execute(_TRUNCATE_SQL)
    return _migrated_db_url
