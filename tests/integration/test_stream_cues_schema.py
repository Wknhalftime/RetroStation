"""Acceptance tests: migration 0029, the ``stream_cues`` table and its rollback (spec: Data)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

import backend.db
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn

ROLLBACK_SQL = Path(backend.db.__file__).parent / "rollback_0029_stream_cues.sql"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def test_columns_types_and_nullability(conn: Conn) -> None:
    rows = conn.execute(
        """SELECT column_name, data_type, is_nullable FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'stream_cues'"""
    ).fetchall()

    assert {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows} == {
        "library_file_id": ("uuid", "NO"),
        "cue_in_ms": ("integer", "NO"),
        "cue_out_ms": ("integer", "NO"),
        "fade_in_ms": ("integer", "NO"),
        "fade_out_ms": ("integer", "NO"),
        "start_next_ms": ("integer", "NO"),
        "loudness_lufs": ("real", "YES"),
        "gain_db": ("real", "NO"),
        "file_size": ("bigint", "YES"),
        "file_mtime_ns": ("bigint", "YES"),
        "analyser_version": ("integer", "NO"),
        "analysis_failed": ("boolean", "NO"),
        "analysed_at": ("timestamp with time zone", "NO"),
    }


def test_primary_key_is_the_library_file(conn: Conn) -> None:
    row = conn.execute(
        """SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint
           WHERE conrelid = 'stream_cues'::regclass AND contype = 'p'"""
    ).fetchone()
    assert row is not None
    assert row["def"] == "PRIMARY KEY (library_file_id)"


def test_library_file_fk_cascades_on_delete(conn: Conn) -> None:
    rows = conn.execute(
        """SELECT confrelid::regclass::text AS target, confdeltype FROM pg_constraint
           WHERE conrelid = 'stream_cues'::regclass AND contype = 'f'"""
    ).fetchall()
    assert [(r["target"], r["confdeltype"]) for r in rows] == [("library_files", "c")]


def test_analysis_failed_defaults_to_false(conn: Conn) -> None:
    file_id = seed.library_file(conn)
    conn.execute(
        """INSERT INTO stream_cues (library_file_id, cue_in_ms, cue_out_ms, fade_in_ms,
               fade_out_ms, start_next_ms, gain_db, analyser_version, analysed_at)
           VALUES (%s, 0, 1000, 0, 0, 0, 0, 1, now())""",
        (file_id,),
    )
    row = conn.execute(
        "SELECT analysis_failed FROM stream_cues WHERE library_file_id = %s", (file_id,)
    ).fetchone()
    assert row == {"analysis_failed": False}


def test_deleting_the_file_deletes_its_cues(conn: Conn) -> None:
    file_id = seed.library_file(conn)
    seed.cue_row(conn, file_id)

    conn.execute("DELETE FROM library_files WHERE id = %s", (file_id,))

    count = conn.execute("SELECT count(*) AS n FROM stream_cues").fetchone()
    assert count == {"n": 0}


def test_rollback_script_drops_the_table_and_forgets_the_version(conn: Conn) -> None:
    def state() -> tuple[bool, bool]:
        row = conn.execute(
            """SELECT to_regclass('public.stream_cues') IS NOT NULL AS has_table,
                      EXISTS (SELECT 1 FROM schema_migrations
                              WHERE version = '0029_stream_cues') AS recorded"""
        ).fetchone()
        assert row is not None
        return row["has_table"], row["recorded"]

    conn.commit()
    with conn.transaction(force_rollback=True):
        conn.execute(ROLLBACK_SQL.read_text(encoding="utf-8"))
        assert state() == (False, False)
    assert state() == (True, True)
