"""Migration 0033 adds stream_cues.orphaned_at (spec D56; D66: "orphaned_at is a nullable
column on stream_cues"). The column's type and nullability are pinned by the re-locked
test_stream_cues_schema.py (D66); this module pins the migration's rollback. Found by name:
the rollback is rollback_*_stream_cues_orphaned_at.sql, which the locked glob
rollback_*_stream_cues.sql does not match."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

import backend.db
from tests.integration.stream_seed import Conn


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def rollback_script() -> Path:
    folder = Path(backend.db.__file__).parent
    [script] = sorted(folder.glob("rollback_*_stream_cues_orphaned_at.sql"))
    return script


def test_orphaned_at_rollback_drops_the_column_and_forgets_the_version(conn: Conn) -> None:
    """Project rule: rollback scripts live in backend/db/ and undo their migration."""

    def state() -> tuple[bool, int]:
        row = conn.execute(
            r"""SELECT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name = 'stream_cues'
                                 AND column_name = 'orphaned_at') AS has_column,
                       (SELECT count(*) FROM schema_migrations
                        WHERE version LIKE '%\_stream\_cues\_orphaned\_at') AS recorded"""
        ).fetchone()
        assert row is not None
        return row["has_column"], row["recorded"]

    conn.commit()
    with conn.transaction(force_rollback=True):
        conn.execute(rollback_script().read_text(encoding="utf-8"))
        assert state() == (False, 0)
    assert state() == (True, 1)
