from pathlib import Path
from typing import Any

import psycopg
import psycopg.pq
import structlog

logger = structlog.get_logger()

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def run_migrations(conn: psycopg.Connection[Any]) -> None:
    """Apply all pending numbered migrations in ascending order.

    Each migration runs inside its own transaction via ``conn.transaction()``
    so that:

    - A successful migration is durable the moment it returns; the runner
      is safe to interrupt mid-batch without losing earlier progress.
    - A failing migration rolls back only its own changes and re-raises.

    To make that contract robust we temporarily flip the connection to
    autocommit mode. In psycopg3, ``conn.transaction()`` on a non-autocommit
    connection creates a *savepoint inside an outer implicit transaction*
    that this function never commits — so without this flip, migrations
    would only persist if the caller remembered to ``conn.commit()`` after
    the call. Silent-success-without-commit from that unspoken contract is
    exactly the bug this audit closes.

    Autocommit is restored to the caller's original setting on exit so the
    connection can be reused normally after migrations complete.
    """
    # Refuse to run if the caller has uncommitted work on this connection —
    # otherwise our autocommit flip would silently persist their mid-
    # transaction state. Treat that as a programming error: callers should
    # commit or rollback before calling run_migrations.
    tx_status = conn.info.transaction_status
    if tx_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError(
            "run_migrations requires an idle connection; caller has a "
            f"pending transaction (status={tx_status.name}). Commit or "
            "rollback before calling."
        )

    # Flip autocommit before any execute — setting autocommit while the
    # connection is in-transaction raises, and we've just verified that
    # isn't the case. Every statement below (_ensure_public_schema,
    # SET search_path, and the migration loop) runs in autocommit mode;
    # the loop's `conn.transaction()` blocks then start real, atomically-
    # committed transactions per migration.
    original_autocommit = conn.autocommit
    try:
        conn.autocommit = True

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
    finally:
        conn.autocommit = original_autocommit


def _ensure_public_schema(conn: psycopg.Connection[Any]) -> None:
    """Recreate public if it was dropped; unqualified CREATE needs a valid
    search_path. Runs under autocommit as part of ``run_migrations``.
    """
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


def _get_applied_versions(conn: psycopg.Connection[Any]) -> set[str]:
    rows = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version ASC"
    ).fetchall()
    return {row[0] for row in rows}
