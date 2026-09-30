"""PG: migration 0032 retires the whole-file hash. The column and its three indexes are gone,
every library row survives the drop, and the rollback script puts the column and indexes back."""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

_DB = Path(__file__).resolve().parents[2] / "backend" / "db"
_MIGRATION = _DB / "migrations" / "0032_drop_file_hash.sql"
_ROLLBACK = _DB / "rollback_0032_drop_file_hash.sql"
_VERSION = "0032_drop_file_hash"
_FILE_HASH_INDEXES = {
    "idx_library_files_file_hash",
    "idx_library_files_unhashed",
    "idx_library_files_unhashed_stat",
}


def _file_hash_column(conn: psycopg.Connection[dict[str, object]]) -> dict[str, object] | None:
    return conn.execute(
        """SELECT data_type, is_nullable FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'library_files'
             AND column_name = 'file_hash'"""
    ).fetchone()


def _library_file_indexes(conn: psycopg.Connection[dict[str, object]]) -> set[str]:
    rows = conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'library_files'"
    ).fetchall()
    return {str(r["indexname"]) for r in rows}


def _applied(conn: psycopg.Connection[dict[str, object]]) -> bool:
    row = conn.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (_VERSION,)).fetchone()
    return row is not None


def test_migration_0032_is_applied_and_the_file_hash_is_gone(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        applied = _applied(conn)
        column = _file_hash_column(conn)
        leftover = _FILE_HASH_INDEXES & _library_file_indexes(conn)

    assert applied
    assert column is None
    assert leftover == set()


def test_the_drop_keeps_every_library_row(migrated_db: str) -> None:
    conn = psycopg.connect(migrated_db, row_factory=dict_row)
    try:
        conn.execute(_ROLLBACK.read_text(encoding="utf-8"))  # the schema before 0032
        conn.execute(
            """INSERT INTO library_files (file_path, file_hash, format, file_size, file_mtime_ns)
               VALUES ('/m/hashed.flac', %s, 'flac', 10, 20),
                      ('/m/plain.mp3', NULL, 'mp3', 30, 40)""",
            ("h" * 64,),
        )
        conn.execute(_MIGRATION.read_text(encoding="utf-8"))
        rows = conn.execute(
            "SELECT file_path, format, file_size, file_mtime_ns FROM library_files"
            " ORDER BY file_path"
        ).fetchall()
    finally:
        conn.rollback()  # DDL is transactional: the schema is left as migrated
        conn.close()

    assert rows == [
        {"file_path": "/m/hashed.flac", "format": "flac", "file_size": 10, "file_mtime_ns": 20},
        {"file_path": "/m/plain.mp3", "format": "mp3", "file_size": 30, "file_mtime_ns": 40},
    ]


def test_the_rollback_script_restores_the_column_and_its_indexes(migrated_db: str) -> None:
    conn = psycopg.connect(migrated_db, row_factory=dict_row)
    try:
        conn.execute(_ROLLBACK.read_text(encoding="utf-8"))
        column = _file_hash_column(conn)
        definitions = {
            str(r["indexname"]): str(r["indexdef"])
            for r in conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'library_files'"
            ).fetchall()
        }
        applied = _applied(conn)
        conn.execute(_MIGRATION.read_text(encoding="utf-8"))  # the drop applies cleanly again
        dropped_again = _file_hash_column(conn) is None
    finally:
        conn.rollback()
        conn.close()

    assert column == {"data_type": "text", "is_nullable": "YES"}
    assert "(file_hash)" in definitions["idx_library_files_file_hash"]
    unhashed = definitions["idx_library_files_unhashed"]
    assert "(file_path)" in unhashed
    assert "(file_hash IS NULL)" in unhashed and "file_status = 'present'" in unhashed
    unhashed_stat = definitions["idx_library_files_unhashed_stat"]
    assert "(file_size, file_mtime_ns)" in unhashed_stat
    assert "(file_hash IS NULL)" in unhashed_stat
    assert not applied
    assert dropped_again
