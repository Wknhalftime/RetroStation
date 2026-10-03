"""Sync connection helper — sets search_path to match the async pool."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from backend.domain.system import StorageUnavailableError


def connect_sync(dsn: str, **kwargs: Any) -> psycopg.Connection[Any]:
    """Open a sync psycopg connection with ``search_path`` set.

    The async pool (``backend.db.pool``) sets ``search_path TO public,
    pg_catalog`` on every connection.  Sync ``psycopg.connect()`` calls
    in task files lack that, causing writes to silently target the wrong
    schema.  This helper ensures parity.
    """
    kwargs.setdefault("row_factory", dict_row)
    conn = psycopg.connect(dsn, **kwargs)
    conn.execute("SET search_path TO public, pg_catalog")
    if not kwargs.get("autocommit"):
        conn.commit()
    return conn


def commit_or_unavailable(conn: psycopg.Connection[Any]) -> None:
    """Commit ``conn``; a connection lost before or during the commit is the domain's
    ``StorageUnavailableError``, so the caller never handles a psycopg exception. Any other
    commit failure passes through unchanged."""
    try:
        conn.commit()
    except psycopg.OperationalError as lost:
        if not conn.closed:
            raise
        raise StorageUnavailableError(f"the commit could not reach the database: {lost}") from lost
