import os
import pytest
import psycopg

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
def migrated_db(clean_db: None, db_url: str) -> str:
    from backend.db.migrations import run_migrations
    with psycopg.connect(db_url) as conn:
        run_migrations(conn)
        conn.commit()
    return db_url
