"""A telemetry adapter's own connection: opened when first used, dropped on any database error.

The matching run's progress row and its pending count are telemetry (spec 2026-10-17): a
database that cannot be reached, a statement or lock timeout, any ``psycopg.Error`` or
``OSError`` must never stop matching. ``TelemetryConnection.run`` gives None instead, as
``ProgressWriter`` drops a write (design note 10, M8): the first failure of an outage logs a
warning, later ones log at debug, and the first statement that lands after it logs one info
line. The broken connection is closed, and the next statement opens a new one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import structlog

from backend.db.progress_writer import Connect

logger = structlog.get_logger()

_DROPPED = (psycopg.Error, OSError)


class TelemetryConnection:
    """Runs statements on a connection of its own; never raises for a database error."""

    def __init__(self, connect: Connect, *, name: str) -> None:
        self._connect = connect
        self._name = name
        self._conn: psycopg.Connection[Any] | None = None
        self._in_outage = False

    def run[T](self, statement: Callable[[psycopg.Connection[Any]], T]) -> T | None:
        """``statement(conn)``, or None when the database could not run it."""
        try:
            if self._conn is None:
                self._conn = self._connect()
            result = statement(self._conn)
        except _DROPPED as error:
            self._note_dropped(error)
            self.close()
            return None
        if self._in_outage:
            logger.info("telemetry_statement_recovered", adapter=self._name)
            self._in_outage = False
        return result

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except _DROPPED as error:  # a broken connection's close can fail; it is gone
            logger.debug("telemetry_close_failed", adapter=self._name, error=str(error))

    def _note_dropped(self, error: BaseException) -> None:
        log = logger.debug if self._in_outage else logger.warning
        log("telemetry_statement_dropped", adapter=self._name, error=str(error))
        self._in_outage = True
