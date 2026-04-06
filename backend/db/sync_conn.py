"""Sync connection helper — sets search_path to match the async pool."""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row


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
