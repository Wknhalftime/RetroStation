import os

import psycopg
import pytest

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://retrostation:retrostation-dev@localhost:5432/retrostation_test",
)


@pytest.fixture(scope="session")
def db_url() -> str:
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def clean_db(db_url: str) -> None:
    """Drop and recreate public schema for a clean slate."""
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


@pytest.fixture
def migrated_db(_migrated_db_url: str) -> str:
    """Per-test fixture: truncates all tables then returns the DB URL."""
    with psycopg.connect(_migrated_db_url, autocommit=True) as conn:
        conn.execute("""
            TRUNCATE log_events, log_identities, log_artists,
                     playlists, broadcast_days, stations,
                     matches, global_mapping_rules,
                     artists, works, recordings,
                     library_files, library_quarantine,
                     song_masters, format_overrides,
                     mb_cache, progress_tracking, user_settings,
                     system_logs
            CASCADE
        """)
    return _migrated_db_url
