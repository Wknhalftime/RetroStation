"""Telemetry's progress rows: a writer on its own connection, and a bounded coverage read.

D77a: the rows go to the existing ``progress_tracking`` table, through the repository's own
upsert statement, so the ``/ws`` feed and the bottom bar show them like any task's.

``ProgressWriter`` (design note 10; D90, D94, M9):

- its connection is its own, autocommit, and does not wait for the disk
  (``writer_options()``: ``synchronous_commit=off``, a 1 s statement and a 0.5 s lock bound);
- each write is one statement, a write-only upsert with no follow-up read (I6), made under a
  lock, so a write still running in a worker thread and the final row never interleave;
- a write the database cannot take (``psycopg.OperationalError``, ``InterfaceError`` or
  ``OSError``) is dropped, never raised: the first drop of an outage logs a warning, later
  drops log at debug, and the first write that lands after it logs one info line. The broken
  connection is closed, and the next write opens a new one;
- ``complete`` writes the final row; a RUNNING write that lands after it is dropped, so a
  cancelled tick that arrives late cannot reopen the row (M9).

``bounded_coverage_read`` (design note 9; I7, PG7): the cue coverage counted on a short-lived
connection of its own per read: autocommit, read-only, and bounded by the D88 numbers (5 s to
connect, 2 s on a lock, 10 s in all). Never the writer's connection and never the run's
transaction. A count that fails or times out gives None, logged once.

``progress_repository``: the progress rows on a connection of the caller's (the meter's
telemetry connection, to end a row a crash left RUNNING, PG13), with a lost database
translated to ``StorageUnavailableError``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import structlog

from backend.db.repositories.stream_cue_coverage import PgCueCoverageRepository
from backend.db.repositories.task_progress import (
    UPSERT_SQL,
    PgTaskProgressRepository,
    upsert_params,
)
from backend.db.sync_conn import connect_sync
from backend.domain.enums import TaskStatus
from backend.domain.streaming import CueCoverage, StreamReadError
from backend.domain.system import StorageUnavailableError, TaskProgress
from backend.repositories.stream_cue_coverage import CoverageRead
from backend.repositories.task_progress import TaskProgressRepository

logger = structlog.get_logger()

__all__ = [
    "COVERAGE_CONNECT_TIMEOUT_S",
    "BoundedCoverageRead",
    "Connect",
    "CoverageRead",
    "ProgressWriter",
    "bounded_coverage_read",
    "coverage_options",
    "progress_repository",
    "writer_options",
]

type Connect = Callable[[], psycopg.Connection[Any]]
"""Opens the writer's connection: ``connect_sync`` with the URL and ``writer_options()``
bound."""

_WRITER_OPTIONS = "-c synchronous_commit=off -c statement_timeout=1000 -c lock_timeout=500"
_COVERAGE_OPTIONS = (
    "-c statement_timeout=10000 -c lock_timeout=2000 -c default_transaction_read_only=on"
)
COVERAGE_CONNECT_TIMEOUT_S = 5
"""D88's connect bound, as the stream service's reads use it."""

_LOST = (psycopg.OperationalError, psycopg.InterfaceError, OSError)
"""What a database that is down, or a connection that broke, raises: a write is dropped."""

_COUNT_FAILED = (*_LOST, StreamReadError)
"""What a coverage read that failed or timed out raises. A statement or lock timeout
(``QueryCanceled``, ``LockNotAvailable``) is an ``OperationalError``; ``StreamReadError`` is
the repository's "the database gave no row"."""


def writer_options() -> str:
    """The writer connection's startup options (D94): commits do not wait for the disk, and a
    statement or a lock wait is bounded, so a telemetry write never stalls its caller."""
    return _WRITER_OPTIONS


def coverage_options() -> str:
    """The coverage read's startup options (I7; the D88 numbers): a 10 s statement bound, a
    2 s lock bound, and read-only."""
    return _COVERAGE_OPTIONS


class ProgressWriter:
    """Writes progress rows on its own connection; never raises for a lost database."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect
        self._conn: psycopg.Connection[Any] | None = None
        self._lock = threading.Lock()
        self._completed = False
        self._closed = False
        self._in_outage = False

    def write(self, task: TaskProgress) -> None:
        """Upsert ``task``; a RUNNING row after ``complete`` or ``close`` is dropped (M9)."""
        with self._lock:
            if self._closed or (self._completed and task.status == TaskStatus.RUNNING):
                logger.debug("progress_write_after_end", task_id=task.task_id)
                return
            self._upsert(task)

    def complete(self, task: TaskProgress) -> None:
        """Upsert the final row; later RUNNING writes are dropped (M9)."""
        with self._lock:
            if self._closed:
                logger.debug("progress_write_after_end", task_id=task.task_id)
                return
            self._upsert(task)
            self._completed = True

    def close(self) -> None:
        """Close the connection; every later write is dropped, so none can open another."""
        with self._lock:
            self._closed = True
            self._drop_connection()

    def _upsert(self, task: TaskProgress) -> None:
        try:
            if self._conn is None:
                self._conn = self._connect()
            self._conn.execute(UPSERT_SQL, upsert_params(task))
        except _LOST as error:
            self._drop_connection()
            self._note_dropped(task, error)
            return
        self._note_landed(task)

    def _note_dropped(self, task: TaskProgress, error: BaseException) -> None:
        """D90: one warning per outage; the rest of the outage at debug."""
        log = logger.debug if self._in_outage else logger.warning
        log("progress_write_dropped", task_id=task.task_id, error=str(error))
        self._in_outage = True

    def _note_landed(self, task: TaskProgress) -> None:
        """One line when the first write after an outage lands; nothing per write."""
        if self._in_outage:
            logger.info("progress_write_recovered", task_id=task.task_id)
            self._in_outage = False

    def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except _LOST as error:  # closing a broken connection can fail; it is gone either way
            logger.debug("progress_writer_close_failed", error=str(error))


class BoundedCoverageRead:
    """A ``CoverageRead``: each call counts the cue coverage on a new bounded, read-only,
    autocommit connection, closed after the read (I7). A count that fails or times out gives
    None: the first of a run of failures logs a warning, the rest log at debug."""

    def __init__(self, url: str, connect: Callable[..., psycopg.Connection[Any]]) -> None:
        self._url = url
        self._connect = connect
        self._failing = False

    def __call__(self) -> CueCoverage | None:
        try:
            with self._connect(
                self._url,
                autocommit=True,
                connect_timeout=COVERAGE_CONNECT_TIMEOUT_S,
                options=coverage_options(),
            ) as conn:
                coverage = PgCueCoverageRepository(conn).coverage()
        except _COUNT_FAILED as error:
            log = logger.debug if self._failing else logger.warning
            log("cue_coverage_read_failed", error=str(error))
            self._failing = True
            return None
        self._failing = False
        return coverage


def bounded_coverage_read(
    url: str, *, connect: Callable[..., psycopg.Connection[Any]] = connect_sync
) -> CoverageRead:
    """The cue coverage read for ``url`` on its own bounded connection per call (I7)."""
    return BoundedCoverageRead(url, connect)


@contextmanager
def progress_repository(connect: Connect) -> Iterator[TaskProgressRepository]:
    """The progress rows on a connection ``connect`` opens, closed after use. A database that
    cannot be reached, or a connection lost on the way, is ``StorageUnavailableError``, so the
    caller never handles a psycopg exception."""
    try:
        with connect() as conn:
            yield PgTaskProgressRepository(conn)
    except _LOST as error:
        raise StorageUnavailableError(f"the progress rows could not be reached: {error}") from error
