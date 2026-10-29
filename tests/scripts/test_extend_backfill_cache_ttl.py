"""scripts/extend_backfill_cache_ttl.py gives the backfill's cache entries the app's lifetime.

The live-version backfill was the one writer of recording-by-mbid entries
that left out the app's lifetime, so its entries, and only its entries, carry
the client's 30-day default. Each is extended to the app's lifetime counted
from when it was cached.
"""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.integration

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
BACKFILL = timedelta(days=30)
APP = timedelta(days=3650)


@pytest.fixture(scope="module")
def extend() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("extend_backfill_cache_ttl")


def _cache_row(
    conn: psycopg.Connection[Any], key: str, lifetime: timedelta, cached_at: datetime = T0
) -> None:
    conn.execute(
        """INSERT INTO mb_cache
               (id, cache_key, entity_type, entity_mbid, response_data, cached_at, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            uuid4(),
            key,
            "recording-search",
            str(uuid4()),
            Jsonb({"id": key}),
            cached_at,
            cached_at + lifetime,
        ),
    )


def _lifetimes(conn: psycopg.Connection[Any]) -> dict[str, timedelta]:
    rows = conn.execute("SELECT cache_key, expires_at - cached_at FROM mb_cache").fetchall()
    return {key: lifetime for key, lifetime in rows}


def test_only_the_backfills_entries_get_the_app_lifetime(
    extend: ModuleType, migrated_db: str
) -> None:
    with psycopg.connect(migrated_db) as conn:
        _cache_row(conn, "recording-by-mbid:backfill", BACKFILL)
        _cache_row(conn, "recording-by-mbid:app", APP)
        _cache_row(conn, "recording:lookup", BACKFILL)

        changed = extend.extend_backfill_entries(conn, ttl_days=3650)
        lifetimes = _lifetimes(conn)

    assert changed == 1
    assert lifetimes == {
        "recording-by-mbid:backfill": APP,
        "recording-by-mbid:app": APP,
        "recording:lookup": BACKFILL,
    }


def test_lifetime_counts_from_when_each_entry_was_cached(
    extend: ModuleType, migrated_db: str
) -> None:
    later = T0 + timedelta(hours=6)
    with psycopg.connect(migrated_db) as conn:
        _cache_row(conn, "recording-by-mbid:first", BACKFILL)
        _cache_row(conn, "recording-by-mbid:last", BACKFILL, cached_at=later)

        extend.extend_backfill_entries(conn, ttl_days=3650)
        expiries = dict(conn.execute("SELECT cache_key, expires_at FROM mb_cache").fetchall())

    assert expiries == {
        "recording-by-mbid:first": T0 + APP,
        "recording-by-mbid:last": later + APP,
    }


def test_a_second_run_changes_nothing(extend: ModuleType, migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        _cache_row(conn, "recording-by-mbid:backfill", BACKFILL)
        extend.extend_backfill_entries(conn, ttl_days=3650)

        assert extend.extend_backfill_entries(conn, ttl_days=3650) == 0


def test_dry_run_counts_the_entries_without_changing_them(
    extend: ModuleType, migrated_db: str
) -> None:
    later = T0 + timedelta(hours=6)
    with psycopg.connect(migrated_db) as conn:
        _cache_row(conn, "recording-by-mbid:first", BACKFILL)
        _cache_row(conn, "recording-by-mbid:last", BACKFILL, cached_at=later)
        _cache_row(conn, "recording-by-mbid:app", APP)

        found = extend.find_backfill_entries(conn)
        lifetimes = _lifetimes(conn)

    assert (found.count, found.first_cached, found.last_cached) == (2, T0, later)
    assert lifetimes["recording-by-mbid:first"] == BACKFILL
