"""Shared low-level utilities for psycopg3-based repository implementations.

These helpers live here rather than in a service module so that the DB layer
never needs to import from ``backend.services``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from functools import wraps
from typing import Any, Protocol

import psycopg

from backend.domain.system import StorageUnavailableError


class _HoldsConnection(Protocol):
    _conn: psycopg.Connection[Any]


def translate_lost_connection[T: _HoldsConnection](
    error: type[StorageUnavailableError],
) -> Callable[[type[T]], type[T]]:
    """Class decorator: each public method re-raises an ``OperationalError`` that left the
    repository's connection closed as ``error``, its subdomain's ``StorageUnavailableError``,
    so callers never handle a psycopg exception.

    An ``OperationalError`` on a live connection (a lock or statement timeout, a deadlock)
    passes through unchanged: ``retry_on_deadlock`` and the tasks' per-item handlers rely on
    its psycopg type, and the connection is still usable.
    """

    def decorate(cls: type[T]) -> type[T]:
        for name, attr in list(vars(cls).items()):
            if not name.startswith("_") and inspect.isfunction(attr):
                setattr(cls, name, _translating(attr, error))
        return cls

    return decorate


def _translating(
    method: Callable[..., object], error: type[StorageUnavailableError]
) -> Callable[..., object]:
    @wraps(method)
    def wrapper(self: _HoldsConnection, /, *args: object, **kwargs: object) -> object:
        try:
            return method(self, *args, **kwargs)
        except psycopg.OperationalError as lost:
            if not self._conn.closed:
                raise
            raise error(f"the database connection was lost: {lost}") from lost

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
