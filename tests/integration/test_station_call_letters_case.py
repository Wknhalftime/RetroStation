"""PG: call letters match in any case, and two stations cannot differ only by case (spec D72:
"Call letters match in any case, and a station differing from another only by case is
blocked (a unique index on the lower-cased call letters ...)"; stored casing is kept as typed;
migration rules: no BEGIN/COMMIT, the rollback lives in backend/db/)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.domain.broadcast import BroadcastStation

_DB = Path(__file__).resolve().parents[2] / "backend" / "db"
_MIGRATION = _DB / "migrations" / "0034_station_call_letters_ci.sql"
_ROLLBACK = _DB / "rollback_0034_station_call_letters_ci.sql"
_VERSION = "0034_station_call_letters_ci"

type Conn = psycopg.Connection[dict[str, object]]


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    """One transaction, rolled back at the end: the schema is left as migrated."""
    connection = psycopg.connect(migrated_db, row_factory=dict_row)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _insert(conn: Conn, call_letters: str) -> None:
    conn.execute("INSERT INTO stations (id, call_letters) VALUES (%s, %s)", (uuid4(), call_letters))


def _case_indexes(conn: Conn) -> list[str]:
    """Definitions of the unique indexes on ``lower(call_letters)``."""
    rows = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'stations'"
    ).fetchall()
    return [
        str(r["indexdef"])
        for r in rows
        if "UNIQUE" in str(r["indexdef"]) and "lower(call_letters)" in str(r["indexdef"])
    ]


def _applied(conn: Conn) -> bool:
    row = conn.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (_VERSION,)).fetchone()
    return row is not None


@pytest.mark.parametrize("asked", ["KIOA", "kioa", "Kioa"])
def test_a_station_is_found_whatever_the_case_of_its_call_letters(conn: Conn, asked: str) -> None:
    stations = PgBroadcastStationRepository(conn)
    stored = stations.create(BroadcastStation(id=uuid4(), call_letters="KIOA"))
    found = stations.get_by_call_letters(asked)
    assert found is not None
    assert (found.id, found.call_letters) == (stored.id, "KIOA")  # the stored casing


@pytest.mark.parametrize("asked", ["KIO", "K_OA", "KIOA%"])
def test_other_call_letters_find_no_station(conn: Conn, asked: str) -> None:
    # Exact apart from case: no prefix match and no LIKE wildcards
    stations = PgBroadcastStationRepository(conn)
    stations.create(BroadcastStation(id=uuid4(), call_letters="KIOA"))
    assert stations.get_by_call_letters(asked) is None


def test_a_station_differing_only_in_case_cannot_be_stored(conn: Conn) -> None:
    _insert(conn, "KIOA")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert(conn, "kioa")


def test_the_case_index_is_unique_on_lower_call_letters(conn: Conn) -> None:
    assert _applied(conn)
    assert len(_case_indexes(conn)) == 1


def test_the_migration_refuses_case_twins_and_names_them(conn: Conn) -> None:
    conn.execute(_ROLLBACK.read_text(encoding="utf-8"))
    _insert(conn, "KIOA")
    _insert(conn, "kioa")
    with pytest.raises(psycopg.errors.UniqueViolation) as refused, conn.transaction():
        conn.execute(_MIGRATION.read_text(encoding="utf-8"))  # in a savepoint
    assert "kioa" in str(refused.value.diag.message_detail)
    assert _case_indexes(conn) == []


def test_the_rollback_drops_the_case_index(conn: Conn) -> None:
    conn.execute(_ROLLBACK.read_text(encoding="utf-8"))
    assert _case_indexes(conn) == []
    assert not _applied(conn)
    _insert(conn, "KIOA")
    _insert(conn, "kioa")  # twins can be stored again
    count = conn.execute("SELECT count(*) AS n FROM stations").fetchone()
    assert count == {"n": 2}
