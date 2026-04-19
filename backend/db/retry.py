"""Cross-cutting retry helpers for database operations.

Keeps retry policy out of individual task/service bodies so every caller
uses the same backoff, logging, and attempt counts.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import psycopg
import structlog

logger = structlog.get_logger()

P = ParamSpec("P")
R = TypeVar("R")


def retry_on_deadlock(
    *,
    max_attempts: int = 3,
    backoff_seconds: float = 0.5,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Retry a callable when Postgres raises ``DeadlockDetected``.

    Delay scales linearly (``backoff_seconds * attempt``). The decorated
    callable must be idempotent on retry: its transaction should be fully
    rolled back before control returns here, so the next attempt starts
    from a clean slate.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except psycopg.errors.DeadlockDetected:
                    if attempt == max_attempts:
                        raise
                    logger.warning(
                        "deadlock_retry",
                        func=func.__qualname__,
                        attempt=attempt,
                        max_attempts=max_attempts,
                    )
                    time.sleep(backoff_seconds * attempt)
            # Unreachable: the loop either returns or raises.
            raise AssertionError("retry_on_deadlock exited loop unexpectedly")

        return wrapper

    return decorator
