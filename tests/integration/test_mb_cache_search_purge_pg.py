"""Migration 0038 drops cached recording-search hits slimmed without a disambiguation.

Other cache entries (direct lookups, artist searches) keep their full payload and stay.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.integration

_VERSION = "0038_purge_slim_recording_search_cache"
_MIGRATION = Path(__file__).resolve().parents[2] / "backend" / "db" / "migrations"
_SQL = (_MIGRATION / f"{_VERSION}.sql").read_text(encoding="utf-8")

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _cache_row(conn: psycopg.Connection[Any], entity_type: str, key: str) -> None:
    conn.execute(
        """INSERT INTO mb_cache
               (id, cache_key, entity_type, entity_mbid, response_data, cached_at, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (uuid4(), key, entity_type, str(uuid4()), Jsonb({"id": key}), T0, T0 + timedelta(days=1)),
    )


def test_migration_0038_is_applied(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s", (_VERSION,)
        ).fetchone()

    assert applied is not None


def test_migration_0038_drops_only_recording_search_hits(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        _cache_row(conn, "recording-search", "recording-by-mbid:a")
        _cache_row(conn, "recording-search", "recording-by-mbid:b")
        _cache_row(conn, "recording", "recording:c")
        _cache_row(conn, "artist", "artist:d")

        conn.execute(_SQL)
        left = conn.execute("SELECT entity_type FROM mb_cache ORDER BY entity_type").fetchall()

    assert [r[0] for r in left] == ["artist", "recording"]
