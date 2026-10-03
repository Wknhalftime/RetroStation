"""Bounded connections for the stream service's reads (D88, user 2026-10-01).

A tune-in reads its station and its station-day schedule before any audio. A lock on a table
those reads touch (``stream_cues`` under a ``LOCK TABLE``, a migration or a VACUUM FULL) used
to hold the read, and the listener, for as long as the lock was held. Each connection here
carries its own lock, statement and connect bounds, so PostgreSQL itself ends the wait and the
worker thread is freed; a database that cannot answer within them is a ``StreamReadError``,
which ``/listen`` answers as ``503 unavailable`` (D14).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
import structlog
from psycopg.rows import DictRow

from backend.db.sync_conn import connect_sync
from backend.domain.streaming import InvalidStreamValueError, StreamReadError
from backend.domain.system import StorageUnavailableError

logger = structlog.get_logger()

READ_ONLY_OPTION = "-c default_transaction_read_only=on"
"""The startup option that makes every transaction on a connection read-only (PG7, I7)."""


@dataclass(frozen=True)
class ReadBounds:
    """How long a stream read may wait to connect, on a lock, and in all.

    ``statement_timeout_ms`` counts lock waits too, so the lock bound must be the smaller.
    Zero means "no limit" to PostgreSQL and libpq, so every bound must be positive.
    libpq treats a ``connect_timeout`` below 2 s as 2 s.
    """

    connect_timeout_s: int
    lock_timeout_ms: int
    statement_timeout_ms: int

    def __post_init__(self) -> None:
        for name in ("connect_timeout_s", "lock_timeout_ms", "statement_timeout_ms"):
            value: int = getattr(self, name)
            if value <= 0:
                raise InvalidStreamValueError(f"ReadBounds.{name} must be > 0, got {value}")
        if self.lock_timeout_ms >= self.statement_timeout_ms:
            raise InvalidStreamValueError(
                f"ReadBounds.lock_timeout_ms ({self.lock_timeout_ms}) must be below "
                f"statement_timeout_ms ({self.statement_timeout_ms})"
            )

    @property
    def options(self) -> str:
        """The bounds as libpq startup options: sent with the connection, no extra round trip."""
        return (
            f"-c lock_timeout={self.lock_timeout_ms} "
            f"-c statement_timeout={self.statement_timeout_ms}"
        )


@contextmanager
def bounded_connection(
    dsn: str, bounds: ReadBounds, *, autocommit: bool = False, read_only: bool = False
) -> Iterator[psycopg.Connection[DictRow]]:
    """A connection whose reads end within ``bounds``. The database being unreachable, a lock
    wait or a statement past its bound, or the connection failing mid-read, is re-raised as
    ``StreamReadError`` and logged; any other error, such as a broken query, passes through.
    A repository that already translated its lost connection (``StorageUnavailableError``)
    counts as the connection failing mid-read.

    ``autocommit`` (PG7): a plain read never needs the extra COMMIT round trip on exit, and
    staying out of a transaction means nothing here can be left idle-in-transaction.
    ``read_only`` adds ``READ_ONLY_OPTION``: a count that must never write (PG7).
    """
    options = f"{bounds.options} {READ_ONLY_OPTION}" if read_only else bounds.options
    try:
        with connect_sync(
            dsn,
            connect_timeout=bounds.connect_timeout_s,
            options=options,
            autocommit=autocommit,
        ) as conn:
            yield conn
    except (psycopg.OperationalError, StorageUnavailableError) as error:
        logger.warning("stream_read_failed", error=repr(error))
        raise StreamReadError(str(error)) from error
