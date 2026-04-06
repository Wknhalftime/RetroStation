from pathlib import Path
from typing import Any

import psycopg
import structlog

logger = structlog.get_logger()

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def run_migrations(conn: psycopg.Connection[Any]) -> None:
    """Apply all pending numbered migrations in ascending order."""
    _ensure_public_schema(conn)
    conn.execute("SET search_path TO public, pg_catalog")
    _ensure_migrations_table(conn)
    applied = _get_applied_versions(conn)

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        logger.warning("No migration files found in %s", MIGRATIONS_DIR)
        return

    for migration_file in migration_files:
        version = migration_file.stem  # e.g. "0001_observation_layer"
        if version in applied:
            logger.debug("Migration %s already applied, skipping", version)
            continue

        logger.info("Applying migration %s", version)
        sql = migration_file.read_text(encoding="utf-8")

        try:
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )
        except Exception as exc:
            logger.error("Migration %s failed: %s", version, exc)
            raise RuntimeError(f"Migration {version} failed: {exc}") from exc

    logger.info("All migrations applied successfully")


def _ensure_public_schema(conn: psycopg.Connection[Any]) -> None:
    """Recreate public if it was dropped; unqualified CREATE needs a valid search_path."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS public")
    conn.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
    conn.execute("GRANT CREATE ON SCHEMA public TO PUBLIC")


def _ensure_migrations_table(conn: psycopg.Connection[Any]) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT        PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    conn.commit()


def _get_applied_versions(conn: psycopg.Connection[Any]) -> set[str]:
    rows = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version ASC"
    ).fetchall()
    return {row[0] for row in rows}
