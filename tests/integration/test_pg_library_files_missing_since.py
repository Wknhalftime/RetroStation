"""PG (spec C1): migration 0031 is applied and adds missing_since; mark_missing stamps it;
relocate and the upsert clear it; the migration stamps rows already missing, and its rollback
undoes it."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.domain.library import LibraryFile

_DB = Path(__file__).resolve().parents[2] / "backend" / "db"
_MIGRATION = _DB / "migrations" / "0031_missing_since.sql"
_ROLLBACK = _DB / "rollback_0031_missing_since.sql"
T_EARLIER = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _row(path: str) -> LibraryFile:
    return LibraryFile(id=uuid4(), file_path=path, format="flac", file_size=1, file_mtime_ns=1)


def test_migration_0031_is_applied_and_adds_missing_since(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = '0031_missing_since'"
        ).fetchone()
        column = conn.execute(
            """SELECT data_type FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'library_files'
                 AND column_name = 'missing_since'"""
        ).fetchone()

    assert applied is not None
    assert column == {"data_type": "timestamp with time zone"}


def test_mark_missing_stamps_the_time_it_went_missing(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        repo.upsert(_row("/m/a.flac"))
        before = conn.execute("SELECT now() AS t").fetchone()  # the transaction's start
        repo.mark_missing("/m/a.flac")
        after = conn.execute("SELECT clock_timestamp() AS t").fetchone()  # the wall clock
        got = repo.get_by_path("/m/a.flac")

    assert before is not None and after is not None and got is not None
    assert got.missing_since is not None
    assert before["t"] <= got.missing_since <= after["t"]


def test_marking_a_row_already_missing_keeps_its_time(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        repo.upsert(_row("/m/a.flac"))
        repo.mark_missing("/m/a.flac")
        conn.execute("UPDATE library_files SET missing_since = %s", (T_EARLIER,))
        repo.mark_missing("/m/a.flac")
        got = repo.get_by_path("/m/a.flac")

    assert got is not None and got.missing_since == T_EARLIER


def test_relocate_clears_missing_since(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        row = repo.upsert(_row("/m/a.flac"))
        repo.mark_missing("/m/a.flac")
        repo.relocate(row.id, "/m/moved/a.flac")
        got = repo.get_by_id(row.id)

    assert got is not None and got.missing_since is None


def test_upsert_of_the_path_clears_missing_since(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        repo.upsert(_row("/m/a.flac"))
        repo.mark_missing("/m/a.flac")
        got = repo.upsert(_row("/m/a.flac"))

    assert got.missing_since is None


def test_upsert_write_only_of_the_path_clears_missing_since(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFileRepository(conn)
        repo.upsert(_row("/m/a.flac"))
        repo.mark_missing("/m/a.flac")
        repo.upsert_write_only(_row("/m/a.flac"))
        got = repo.get_by_path("/m/a.flac")

    assert got is not None and got.missing_since is None


def test_the_migration_stamps_rows_already_missing(migrated_db: str) -> None:
    conn = psycopg.connect(migrated_db, row_factory=dict_row)
    try:
        conn.execute(_ROLLBACK.read_text(encoding="utf-8"))  # the schema before 0031
        conn.execute(
            "INSERT INTO library_files (file_path, format, file_status) VALUES "
            "('/m/gone.flac', 'flac', 'missing'), ('/m/here.flac', 'flac', 'present')"
        )
        conn.execute(_MIGRATION.read_text(encoding="utf-8"))
        rows = conn.execute(
            "SELECT file_path, missing_since = now() AS stamped FROM library_files ORDER BY 1"
        ).fetchall()
    finally:
        conn.rollback()  # DDL is transactional: the schema is left as migrated
        conn.close()

    assert [(r["file_path"], r["stamped"]) for r in rows] == [
        ("/m/gone.flac", True),
        ("/m/here.flac", None),
    ]


def test_the_rollback_script_undoes_the_migration(migrated_db: str) -> None:
    conn = psycopg.connect(migrated_db, row_factory=dict_row)
    try:
        conn.execute(_ROLLBACK.read_text(encoding="utf-8"))
        column = conn.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_name = 'library_files' AND column_name = 'missing_since'"""
        ).fetchone()
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = '0031_missing_since'"
        ).fetchone()
        conn.execute(_MIGRATION.read_text(encoding="utf-8"))  # nothing it creates is left over
    finally:
        conn.rollback()
        conn.close()

    assert (column, applied) == (None, None)
