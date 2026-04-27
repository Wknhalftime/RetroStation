import psycopg
import pytest


def test_all_migrations_applied(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    versions = [r[0] for r in rows]
    assert len(versions) == 23
    assert versions[0].startswith("0001")
    assert versions[-1].startswith("0023")


def test_station_delete_cascade_fks(migrated_db: str) -> None:
    """Migration 0018 must make station-delete-path FKs ON DELETE CASCADE."""
    expected = {
        "broadcast_days_station_id_fkey",
        "playlists_station_id_fkey",
        "play_events_broadcast_day_id_fkey",
    }
    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute("""
            SELECT conname, confdeltype FROM pg_constraint
            WHERE contype = 'f'
              AND conname = ANY(%s)
        """, (list(expected),)).fetchall()
    fk_map = {r[0]: r[1] for r in rows}
    missing = expected - fk_map.keys()
    assert not missing, f"Missing FK constraints: {missing}"
    wrong = {k: v for k, v in fk_map.items() if v != "c"}
    assert not wrong, f"Wrong delete action (expected 'c' for CASCADE): {wrong}"


def test_all_expected_tables_exist(migrated_db: str) -> None:
    expected = {
        "playlists", "broadcast_artists", "track_identities", "play_events",
        "artists", "works", "recordings",
        "matches", "mapping_rules",
        "library_files", "library_quarantine",
        "user_settings", "system_logs", "progress_tracking",
        "stations", "broadcast_days",
        "song_masters", "format_overrides",
        "mb_cache",
        "library_folders", "library_folder_staged_hashes",
    }
    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
        """).fetchall()
    actual = {r[0] for r in rows}
    missing = expected - actual
    assert not missing, f"Missing tables: {missing}"


def test_pgvector_extension_installed(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        row = conn.execute(
            "SELECT extname FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    assert row is not None, "pgvector extension not installed"


def test_embedding_columns_on_four_tables(migrated_db: str) -> None:
    tables = ["broadcast_artists", "track_identities", "works", "recordings"]
    with psycopg.connect(migrated_db) as conn:
        for table in tables:
            row = conn.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s AND column_name = 'embedding'
            """, (table,)).fetchone()
            assert row is not None, f"{table}.embedding column missing"


def test_deferred_fk_columns_exist(migrated_db: str) -> None:
    checks = [
        ("playlists",  "station_id"),
        ("play_events", "broadcast_day_id"),
    ]
    with psycopg.connect(migrated_db) as conn:
        for table, column in checks:
            row = conn.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s AND column_name = %s
            """, (table, column)).fetchone()
            assert row is not None, f"{table}.{column} missing"


def test_matches_library_file_fk_constraint_exists(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        row = conn.execute("""
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'matches'
              AND constraint_name = 'fk_matches_library_file'
        """).fetchone()
    assert row is not None, "matches.fk_matches_library_file constraint missing"


def test_migrations_idempotent(migrated_db: str) -> None:
    """Running migrations a second time must be a no-op."""
    from backend.db.migrations import run_migrations
    with psycopg.connect(migrated_db) as conn:
        run_migrations(conn)  # second run — must not raise


def test_xor_constraint_on_matches(migrated_db: str) -> None:
    """The XOR constraint on matches must reject rows with both FKs set."""
    import uuid
    with (
        psycopg.connect(migrated_db) as conn,
        pytest.raises(psycopg.errors.CheckViolation),
        conn.transaction(),
    ):
        conn.execute("""
                INSERT INTO matches (identity_id, artist_id, confidence_score, match_tier)
                VALUES (%s, %s, 0.9, 'MANUAL')
            """, (uuid.uuid4(), uuid.uuid4()))


def test_runner_persists_even_when_caller_rolls_back_afterwards(
    db_url: str,
) -> None:
    """Regression for the silent-success audit.

    Simulates a caller that runs migrations on a non-autocommit connection
    and then rolls back. With the old code, conn.transaction() created
    savepoints inside an uncommitted outer transaction — the caller's
    rollback would discard every migration. With the fix, each migration
    is committed inside run_migrations via autocommit-mode transactions,
    so the caller's rollback afterwards is a no-op.

    Note: we open the connection WITHOUT a context manager because
    ``with psycopg.connect(...) as conn:`` implicitly commits on normal
    exit — which would mask the bug. Rollback is the strongest assertion
    that the runner's persistence does not depend on caller commit.
    """
    import psycopg

    from backend.db.migrations import run_migrations

    # Start from a clean schema so run_migrations has work to do.
    with psycopg.connect(db_url, autocommit=True) as setup:
        setup.execute("DROP SCHEMA IF EXISTS public CASCADE")
        setup.execute("CREATE SCHEMA public")

    conn = psycopg.connect(db_url, autocommit=False)
    try:
        run_migrations(conn)
        # Deliberate rollback — would discard work still in an uncommitted
        # outer transaction. If the runner relied on caller-side commit,
        # the schema would vanish here.
        conn.rollback()
    finally:
        conn.close()

    # Verify migrations persisted across the rollback + close.
    with psycopg.connect(db_url) as verify:
        rows = verify.execute(
            "SELECT version FROM schema_migrations ORDER BY version",
        ).fetchall()
    assert len(rows) >= 1, (
        "run_migrations must commit each migration internally; a caller "
        "rollback afterwards must not discard them"
    )


def test_runner_rejects_caller_with_open_transaction(db_url: str) -> None:
    """Runner must refuse to run when the caller has uncommitted work.

    Silently committing the caller's in-progress transaction (or flipping
    autocommit on a connection in INTRANS state) would be a subtle API
    violation — a helper that incidentally flushes unrelated pending
    writes as a side effect of applying migrations. The runner raises
    RuntimeError instead, leaving the caller's transaction intact so
    they can commit or rollback explicitly.
    """
    import psycopg
    import pytest

    from backend.db.migrations import run_migrations

    conn = psycopg.connect(db_url, autocommit=False)
    try:
        # Introduce a pending write — implicitly starts a transaction.
        conn.execute("CREATE TABLE IF NOT EXISTS _mig_audit_tmp (id int)")
        with pytest.raises(RuntimeError, match="pending transaction"):
            run_migrations(conn)
    finally:
        conn.rollback()
        conn.close()


def test_runner_restores_caller_autocommit_mode(migrated_db: str) -> None:
    """Runner must restore the caller's original autocommit setting.

    Temporarily flipping autocommit is how the runner makes each migration
    durable; the caller may still expect a non-autocommit connection for
    subsequent work. Restoring the original value keeps the function
    transparent to callers.
    """
    import psycopg

    from backend.db.migrations import run_migrations

    with psycopg.connect(migrated_db, autocommit=False) as conn:
        assert conn.autocommit is False
        run_migrations(conn)
        assert conn.autocommit is False, (
            "run_migrations must restore autocommit=False after it completes"
        )

    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert conn.autocommit is True
        run_migrations(conn)
        assert conn.autocommit is True, (
            "run_migrations must restore autocommit=True after it completes"
        )
