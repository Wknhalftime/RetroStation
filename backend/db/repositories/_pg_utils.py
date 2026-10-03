"""Shared low-level utilities for psycopg3-based repository implementations.

These helpers live here rather than in a service module so that the DB layer
never needs to import from ``backend.services``.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, Concatenate, Protocol

import psycopg

from backend.domain.system import StorageUnavailableError


class _HoldsConnection(Protocol):
    _conn: psycopg.Connection[Any]


def translate_lost_connection[Repo: _HoldsConnection, **P, R](
    method: Callable[Concatenate[Repo, P], R],
) -> Callable[Concatenate[Repo, P], R]:
    """Re-raise an ``OperationalError`` that left the repository's connection closed as the
    domain's ``StorageUnavailableError``, so callers never handle a psycopg exception.

    An ``OperationalError`` on a live connection (a lock or statement timeout, a deadlock)
    passes through unchanged: ``retry_on_deadlock`` and the tasks' per-item handlers rely on
    its psycopg type, and the connection is still usable.
    """

    @wraps(method)
    def wrapper(self: Repo, /, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(self, *args, **kwargs)
        except psycopg.OperationalError as error:
            if not self._conn.closed:
                raise
            raise StorageUnavailableError(f"the database connection was lost: {error}") from error

    return wrapper


def parse_embedding(raw: Any) -> list[float] | None:
    """Convert a pgvector column value to a Python list of floats.

    pgvector may return the value as an already-parsed Python list or as a
    bracketed string such as ``"[0.1,0.2,0.3]"``.  Both forms are handled
    gracefully; ``None`` is returned when the column is NULL.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    return [float(x) for x in str(raw).strip("[]").split(",")]


def format_embedding(embedding: list[float]) -> str:
    """Serialize a Python float list to the pgvector literal format.

    Example: ``[0.1, 0.2, 0.3]`` → ``"[0.1,0.2,0.3]"``
    """
    return "[" + ",".join(str(v) for v in embedding) + "]"
