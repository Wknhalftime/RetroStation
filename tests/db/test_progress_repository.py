"""The progress rows on a caller's connection (PR G2, Task 8; not a locked test).

Requirements:
- PG13, M10: at start-up the lifespan ends a meter row a crash left RUNNING on the meter's
  telemetry connection; a database that cannot be reached must not stop start-up, so the
  adapter translates a lost database into the domain's ``StorageUnavailableError`` (error
  handling at its own layer: the caller never handles a psycopg exception);
- the connection is closed after use.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from backend.db.progress_writer import progress_repository
from backend.domain.system import StorageUnavailableError


class LostConn:
    """A connection whose every statement finds the database gone."""

    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> LostConn:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def execute(self, query: object, params: object = None) -> Any:
        raise psycopg.OperationalError("server closed the connection unexpectedly")


@pytest.mark.parametrize(
    "error",
    [psycopg.OperationalError("refused"), psycopg.InterfaceError("closed"), OSError("down")],
    ids=["operational", "interface", "os"],
)
def test_a_database_that_cannot_be_reached_is_storage_unavailable(error: Exception) -> None:
    def connect() -> Any:
        raise error

    with pytest.raises(StorageUnavailableError), progress_repository(connect):
        pass


def test_a_connection_lost_on_the_way_is_storage_unavailable_and_closed() -> None:
    conn = LostConn()
    with pytest.raises(StorageUnavailableError), progress_repository(lambda: conn) as repo:
        repo.get_by_id("stream_resources")
    assert conn.closed


def test_any_other_error_passes_through() -> None:
    conn = LostConn()
    with pytest.raises(KeyError), progress_repository(lambda: conn):
        raise KeyError("not a database error")
    assert conn.closed
