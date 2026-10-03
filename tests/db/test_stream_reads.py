"""The stream service's read bounds (D88, user 2026-10-01): the values the app is wired with,
their validation, and a database that cannot be reached at all. The bounds' effect on a live
database is in ``tests/integration/test_stream_read_bounds.py``."""

from __future__ import annotations

import socket

import pytest

from backend.db.stream_reads import ReadBounds, bounded_connection
from backend.domain.streaming import InvalidStreamValueError, StreamReadError


def test_the_app_bounds_its_stream_reads_as_decided() -> None:
    """D88: a lock wait is cut at 2 s (the realistic cause), a statement at 10 s (a backstop
    far above a cold day read, D40), a connection attempt at 5 s. Imported here so that a
    rename in the composition root fails this test alone."""
    from backend.main import STREAM_READ_BOUNDS

    decided = ReadBounds(connect_timeout_s=5, lock_timeout_ms=2_000, statement_timeout_ms=10_000)
    assert decided == STREAM_READ_BOUNDS


@pytest.mark.parametrize(
    "fields",
    [
        {"connect_timeout_s": 0},
        {"lock_timeout_ms": 0},
        {"statement_timeout_ms": -1},
    ],
    ids=["no connect bound", "no lock bound", "negative statement bound"],
)
def test_a_bound_must_be_positive(fields: dict[str, int]) -> None:
    """0 means "no limit" to PostgreSQL and libpq, so it is refused, not passed on."""
    bounds = {"connect_timeout_s": 5, "lock_timeout_ms": 2_000, "statement_timeout_ms": 10_000}
    with pytest.raises(InvalidStreamValueError):
        ReadBounds(**(bounds | fields))


def test_a_lock_bound_must_be_below_the_statement_bound() -> None:
    """A statement's time includes its lock waits, so a lock bound at or above the statement
    bound would never act."""
    with pytest.raises(InvalidStreamValueError):
        ReadBounds(connect_timeout_s=5, lock_timeout_ms=10_000, statement_timeout_ms=10_000)


def _closed_port() -> int:
    """A loopback port nothing listens on: bound, read, then released."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.slow  # on Windows a refused loopback connection can wait out the connect bound
def test_a_database_that_cannot_be_reached_is_a_read_failure() -> None:
    dsn = f"postgresql://nobody@127.0.0.1:{_closed_port()}/none"
    bounds = ReadBounds(connect_timeout_s=2, lock_timeout_ms=2_000, statement_timeout_ms=10_000)
    with pytest.raises(StreamReadError), bounded_connection(dsn, bounds):
        pass
