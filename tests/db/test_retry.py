"""Unit tests for the retry_on_deadlock decorator.

The decorator is the reusable replacement for the inline try/except/sleep
loop that used to live in ingestion_task. It must:
- Return the function's result on first success.
- Retry on psycopg.errors.DeadlockDetected up to max_attempts times.
- Not retry on other exceptions.
- Re-raise the last DeadlockDetected once the attempts are exhausted.
- Reject invalid configuration.
"""
from __future__ import annotations

import psycopg
import pytest

from backend.db.retry import retry_on_deadlock


def _make_deadlock() -> psycopg.errors.DeadlockDetected:
    return psycopg.errors.DeadlockDetected("simulated")


def test_returns_value_when_no_deadlock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.db.retry.time.sleep", lambda _s: None)
    calls: list[int] = []

    @retry_on_deadlock(max_attempts=3, backoff_seconds=0)
    def ok() -> str:
        calls.append(1)
        return "done"

    assert ok() == "done"
    assert len(calls) == 1


def test_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("backend.db.retry.time.sleep", sleeps.append)
    calls: list[int] = []

    @retry_on_deadlock(max_attempts=3, backoff_seconds=0.1)
    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise _make_deadlock()
        return "ok"

    assert flaky() == "ok"
    assert len(calls) == 3
    # Linear backoff: sleeps after attempts 1 and 2, scaled by attempt number.
    assert sleeps == [0.1, 0.2]


def test_reraises_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.db.retry.time.sleep", lambda _s: None)
    calls: list[int] = []

    @retry_on_deadlock(max_attempts=2, backoff_seconds=0)
    def always_deadlocks() -> None:
        calls.append(1)
        raise _make_deadlock()

    with pytest.raises(psycopg.errors.DeadlockDetected):
        always_deadlocks()
    assert len(calls) == 2


def test_does_not_retry_unrelated_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.db.retry.time.sleep", lambda _s: None)
    calls: list[int] = []

    @retry_on_deadlock(max_attempts=3, backoff_seconds=0)
    def raises_value_error() -> None:
        calls.append(1)
        raise ValueError("unrelated")

    with pytest.raises(ValueError, match="unrelated"):
        raises_value_error()
    assert len(calls) == 1


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        retry_on_deadlock(max_attempts=0)
