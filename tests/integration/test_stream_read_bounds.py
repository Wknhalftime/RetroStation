"""The stream service's reads are bounded, against PostgreSQL (D88, user 2026-10-01: a tune-in
whose schedule cannot be read in time answers ``503 unavailable`` rather than hanging; D14).

A lock is the realistic cause (PR E2's manual checks: an ACCESS EXCLUSIVE lock on
``stream_cues`` stalled a fresh ``/listen`` for ~42 s), so a lock wait is cut short at 2 s; a
statement bound of 10 s is the backstop, far above a cold day read (D40); a connection that
cannot be made in 5 s fails too. Each failure is the domain's ``StreamReadError``, logged.

The test database itself sets ``lock_timeout = 5s`` and ``statement_timeout = 15s``
(``tests/conftest.py``), so every bound asserted here is below those and is the connection's own.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from psycopg.rows import dict_row
from structlog.testing import capture_logs

from backend.config import Settings
from backend.db.repositories.playable_schedule import PgPlayableScheduleRepository
from backend.db.stream_reads import ReadBounds, bounded_connection
from backend.domain.streaming import StreamReadError
from backend.routers import listen
from backend.services.streaming.bookmarks import BookmarkStore
from backend.services.streaming.service import StreamService, StreamServiceConfig
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn
from tests.services.streaming.helpers import EngineStarts
from tests.services.streaming.schedule import BACKEND

pytestmark = pytest.mark.integration

TIGHT = ReadBounds(connect_timeout_s=5, lock_timeout_ms=300, statement_timeout_ms=1_500)
"""Bounds short enough to test quickly, each below the test database's own."""

DAY = date(1995, 3, 14)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row, autocommit=True) as connection:
        yield connection


@pytest.fixture
def locked_cues(migrated_db: str) -> Iterator[None]:
    """Another session holds ``stream_cues`` in ACCESS EXCLUSIVE mode until the test ends,
    as a ``LOCK TABLE`` (or a migration, or a VACUUM FULL) would."""
    with psycopg.connect(migrated_db) as locker:
        locker.execute("LOCK TABLE stream_cues IN ACCESS EXCLUSIVE MODE")
        yield
        locker.rollback()


def test_a_connection_carries_its_bounds(migrated_db: str) -> None:
    with bounded_connection(migrated_db, TIGHT) as connection:
        lock = connection.execute("SHOW lock_timeout").fetchone()
        statement = connection.execute("SHOW statement_timeout").fetchone()
    assert (lock, statement) == ({"lock_timeout": "300ms"}, {"statement_timeout": "1500ms"})


def test_a_day_read_blocked_by_a_lock_fails_at_the_lock_bound(
    conn: Conn, migrated_db: str, locked_cues: None
) -> None:
    station = seed.station(conn)
    started = time.monotonic()
    with (
        capture_logs() as logs,
        pytest.raises(StreamReadError),
        bounded_connection(migrated_db, TIGHT) as connection,
    ):
        PgPlayableScheduleRepository(connection).get_day(station, DAY)
    assert time.monotonic() - started < 2.0  # the 0.3 s bound, not the database's 5 s
    assert [e["log_level"] for e in logs if e["event"] == "stream_read_failed"] == ["warning"]


def test_a_read_past_the_statement_bound_fails(migrated_db: str) -> None:
    started = time.monotonic()
    with pytest.raises(StreamReadError), bounded_connection(migrated_db, TIGHT) as connection:
        connection.execute("SELECT pg_sleep(5)")
    assert time.monotonic() - started < 4.0  # the 1.5 s bound, not the database's 15 s


def test_a_mistake_in_a_query_is_not_a_read_failure(migrated_db: str) -> None:
    """Only the database being unavailable in time is a ``StreamReadError``; a broken query
    is a bug and stays one."""
    with (
        pytest.raises(psycopg.errors.UndefinedTable),
        bounded_connection(migrated_db, TIGHT) as connection,
    ):
        connection.execute("SELECT * FROM no_such_table")


# ---- the requirement, through the app ------------------------------------------------------


def _station_call_letters(conn: Conn) -> str:
    station = seed.station(conn)
    row = conn.execute("SELECT call_letters FROM stations WHERE id = %s", (station,)).fetchone()
    assert row is not None
    return str(row["call_letters"])


@pytest.mark.slow
async def test_a_tune_in_against_a_locked_schedule_answers_503_unavailable(
    conn: Conn, migrated_db: str, locked_cues: None, tmp_path: Path
) -> None:
    """The wired service (``main.build_stream_ports``) reads the station, then the day; the
    day read waits on the lock, gives up at the production 2 s bound, and ``/listen`` answers
    ``503 unavailable``: well inside the test database's own 5 s lock bound."""
    from backend.main import build_stream_ports

    call = _station_call_letters(conn)
    settings = Settings(_env_file=None, database_url=migrated_db)  # type: ignore[call-arg]
    service = StreamService(
        build_stream_ports(settings, EngineStarts()),
        BookmarkStore(),
        StreamServiceConfig(callback_base_url=BACKEND, log_dir=tmp_path / "logs"),
    )
    app = FastAPI()
    app.include_router(listen.router)
    app.state.stream_service = service
    transport = httpx.ASGITransport(app=app, client=("192.168.1.30", 50123))
    started = time.monotonic()
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get(f"/listen/{call}/{DAY.year}?key=car")
    elapsed = time.monotonic() - started
    assert (response.status_code, response.json()) == (503, {"detail": "unavailable"})
    assert 1.5 <= elapsed < 4.5, elapsed
    assert service.open_sessions == 0
