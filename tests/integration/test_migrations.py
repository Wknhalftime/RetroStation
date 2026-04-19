import psycopg
import pytest


def test_all_migrations_applied(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    versions = [r[0] for r in rows]
    assert len(versions) == 18
    assert versions[0].startswith("0001")
    assert versions[-1].startswith("0018")


def test_station_delete_cascade_fks(migrated_db: str) -> None:
    """Migration 0018 must make station-delete-path FKs ON DELETE CASCADE."""
    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute("""
            SELECT conname, confdeltype FROM pg_constraint
            WHERE contype = 'f'
              AND conname IN (
                  'broadcast_days_station_id_fkey',
                  'playlists_station_id_fkey',
                  'play_events_broadcast_day_id_fkey'
              )
        """).fetchall()
    fk_map = {r[0]: r[1] for r in rows}
    assert len(fk_map) == 3, f"Missing FK constraints: {fk_map}"
    assert all(v == "c" for v in fk_map.values()), f"Wrong delete action: {fk_map}"


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
