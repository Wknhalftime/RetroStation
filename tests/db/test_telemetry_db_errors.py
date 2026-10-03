"""Telemetry's adapters never raise a database error (PR G2 final review M8; not locked).

Requirements:
- I2, design notes 9 and 10: the progress row and the coverage count are telemetry; they
  never change what the cue run stores, and never stop the meter. Any ``psycopg.Error`` the
  database raises (not only a lost connection: a ``DataError``, an ``InternalError``, an
  ``IntegrityError``...) is a dropped write or a count of None, logged once per outage: the
  first at warning, the rest at debug;
- a dropped final write (``run_ended`` in the cue run's ``finally``) cannot mask the run's own
  error, because it does not raise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

from backend.db.progress_writer import ProgressWriter, bounded_coverage_read
from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
ROW = TaskProgress("run-1", TaskType.CUE_ANALYSIS, TaskStatus.RUNNING, {}, T0, T0)
LOUD = {"warning", "error", "critical"}

ERRORS = [
    psycopg.DataError("invalid input syntax for type json"),
    psycopg.InternalError("could not read block"),
    psycopg.IntegrityError("duplicate key value"),
    psycopg.ProgrammingError("column does not exist"),
    psycopg.errors.DiskFull("could not extend file"),
]
IDS = ["data", "internal", "integrity", "programming", "disk-full"]


class FailingConn:
    """A connection whose every statement raises ``error``."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.closed = False

    def __enter__(self) -> FailingConn:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def execute(self, query: object, params: object = None) -> Any:
        raise self.error

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("error", ERRORS, ids=IDS)
def test_the_writer_drops_any_database_error(error: Exception) -> None:
    opened: list[FailingConn] = []

    def connect() -> Any:
        opened.append(FailingConn(error))
        return opened[-1]

    writer = ProgressWriter(connect)
    with capture_logs() as logs:
        writer.write(ROW)
        writer.write(ROW)
        writer.complete(ROW)
    dropped = [e["log_level"] for e in logs if e["event"] == "progress_write_dropped"]
    assert dropped == ["warning", "debug", "debug"]
    assert len([e for e in logs if e["log_level"] in LOUD]) == 1
    # Each broken connection is closed; the next write opens a new one.
    assert len(opened) == 3 and all(conn.closed for conn in opened)


@pytest.mark.parametrize("error", ERRORS, ids=IDS)
def test_a_close_that_fails_with_any_database_error_is_dropped(error: Exception) -> None:
    class BadClose(FailingConn):
        def execute(self, query: object, params: object = None) -> Any:
            return None

        def close(self) -> None:
            raise error

    writer = ProgressWriter(lambda: BadClose(error))
    writer.write(ROW)
    writer.close()  # does not raise


@pytest.mark.parametrize("error", ERRORS, ids=IDS)
def test_the_coverage_read_gives_none_for_any_database_error(error: Exception) -> None:
    opened: list[FailingConn] = []

    def connect(*args: object, **kwargs: object) -> Any:
        opened.append(FailingConn(error))
        return opened[-1]

    read = bounded_coverage_read("postgresql://unused", connect=connect)
    with capture_logs() as logs:
        assert read() is None
        assert read() is None
    failed = [e["log_level"] for e in logs if e["event"] == "cue_coverage_read_failed"]
    assert failed == ["warning", "debug"]
    assert all(conn.closed for conn in opened)
