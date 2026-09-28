"""Acceptance tests: the ``stream_cues`` table and its rollback (spec: Data, D20).

Keyed by ``audio_hash`` with the same format rule as ``library_files.audio_hash``; no
foreign key (a hash is not unique in the library, and a row may outlive its files); no file
identity or stat columns. No migration number appears here: the migration and its rollback
are found by name.

DRAFT for D20: replaces ``test_stream_cues_schema.py`` once the user approves.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

import backend.db
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn

HEX32 = "0123456789abcdef" * 2


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def rollback_script() -> Path:
    [script] = sorted(Path(backend.db.__file__).parent.glob("rollback_*_stream_cues.sql"))
    return script


def test_columns_types_and_nullability(conn: Conn) -> None:
    rows = conn.execute(
        """SELECT column_name, data_type, is_nullable FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'stream_cues'"""
    ).fetchall()

    assert {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows} == {
        "audio_hash": ("text", "NO"),
        "cue_in_ms": ("integer", "NO"),
        "cue_out_ms": ("integer", "NO"),
        "fade_in_ms": ("integer", "NO"),
        "fade_out_ms": ("integer", "NO"),
        "start_next_ms": ("integer", "NO"),
        "loudness_lufs": ("real", "YES"),
        "gain_db": ("real", "NO"),
        "analyser_version": ("integer", "NO"),
        "analysis_failed": ("boolean", "NO"),
        "analysed_at": ("timestamp with time zone", "NO"),
    }


def test_primary_key_is_the_audio_hash(conn: Conn) -> None:
    row = conn.execute(
        """SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint
           WHERE conrelid = 'stream_cues'::regclass AND contype = 'p'"""
    ).fetchone()
    assert row is not None
    assert row["def"] == "PRIMARY KEY (audio_hash)"


def test_has_no_foreign_key(conn: Conn) -> None:
    rows = conn.execute(
        """SELECT conname FROM pg_constraint
           WHERE conrelid = 'stream_cues'::regclass AND contype = 'f'"""
    ).fetchall()
    assert rows == []


def _accepted(conn: Conn, statement: str, params: tuple[object, ...]) -> bool:
    """Whether ``statement`` succeeds; a CHECK violation is rolled back to a savepoint."""
    try:
        with conn.transaction():
            conn.execute(statement, params)
    except psycopg.errors.CheckViolation:
        return False
    return True


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (f"flac-md5:{HEX32}", True),
        (f"audio-sha256:{HEX32 * 2}", True),
        (f"flac-md5:{HEX32.upper()}", False),
        (f"flac-md5:{HEX32[:-1]}", False),
        (f"audio-sha256:{HEX32}", False),
        (f"md5:{HEX32}", False),
        (HEX32, False),
        ("", False),
    ],
    ids=["flac-md5", "audio-sha256", "upper-hex", "short", "wrong-length", "kind", "bare", "empty"],
)
def test_audio_hash_format_is_the_library_format(conn: Conn, value: str, valid: bool) -> None:
    """``stream_cues`` accepts exactly the hashes ``library_files.audio_hash`` accepts."""
    file_id = seed.library_file(conn)
    in_library = _accepted(
        conn, "UPDATE library_files SET audio_hash = %s WHERE id = %s", (value, file_id)
    )
    in_cues = _accepted(
        conn,
        """INSERT INTO stream_cues (audio_hash, cue_in_ms, cue_out_ms, fade_in_ms,
               fade_out_ms, start_next_ms, gain_db, analyser_version, analysed_at)
           VALUES (%s, 0, 1000, 0, 0, 0, 0, 1, now())""",
        (value,),
    )

    assert (in_cues, in_library) == (valid, valid)


def test_analysis_failed_defaults_to_false(conn: Conn) -> None:
    audio = seed.audio_hash()
    conn.execute(
        """INSERT INTO stream_cues (audio_hash, cue_in_ms, cue_out_ms, fade_in_ms,
               fade_out_ms, start_next_ms, gain_db, analyser_version, analysed_at)
           VALUES (%s, 0, 1000, 0, 0, 0, 0, 1, now())""",
        (audio,),
    )
    row = conn.execute(
        "SELECT analysis_failed FROM stream_cues WHERE audio_hash = %s", (audio,)
    ).fetchone()
    assert row == {"analysis_failed": False}


def test_deleting_the_last_file_of_an_audio_keeps_its_cues(conn: Conn) -> None:
    """A row for audio no longer in the library is harmless (D20); streaming prunes it."""
    file_id, audio = seed.cued_file(conn)

    conn.execute("DELETE FROM library_files WHERE id = %s", (file_id,))

    row = conn.execute(
        "SELECT count(*) AS n FROM stream_cues WHERE audio_hash = %s", (audio,)
    ).fetchone()
    assert row == {"n": 1}


def test_rollback_script_drops_the_table_and_forgets_the_version(conn: Conn) -> None:
    def state() -> tuple[bool, int]:
        row = conn.execute(
            r"""SELECT to_regclass('public.stream_cues') IS NOT NULL AS has_table,
                       (SELECT count(*) FROM schema_migrations
                        WHERE version LIKE '%\_stream\_cues') AS recorded"""
        ).fetchone()
        assert row is not None
        return row["has_table"], row["recorded"]

    conn.commit()
    with conn.transaction(force_rollback=True):
        conn.execute(rollback_script().read_text(encoding="utf-8"))
        assert state() == (False, 0)
    assert state() == (True, 1)
