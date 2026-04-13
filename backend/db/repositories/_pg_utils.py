"""Shared low-level utilities for psycopg3-based repository implementations.

These helpers live here rather than in a service module so that the DB layer
never needs to import from ``backend.services``.
"""
from __future__ import annotations

from typing import Any


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

