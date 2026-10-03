"""One set of bounds for both coverage reads (PR G2 final review T7-b; not a locked test).

Requirements:
- PG7, I7 (the D88 numbers): the cue coverage is counted on a connection of its own, bounded
  at 5 s to connect, 2 s on a lock and 10 s in all, and read-only;
- one ``ReadBounds`` constant in ``backend/db`` holds those numbers: the cue run's count
  (``coverage_options()``) and the route's count (``dependencies.COVERAGE_READ_BOUNDS``)
  both build from it, so the two cannot drift apart;
- the route's connection is read-only too; the two adapters keep their own failure contracts
  (the route's raises ``StreamReadError``, answered 503; the run's gives None).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from backend import dependencies
from backend.db import progress_writer, stream_reads
from backend.db.repositories import stream_cue_coverage
from backend.db.repositories.stream_cue_coverage import (
    COVERAGE_READ_BOUNDS,
    BoundedCueCoverageRepository,
)
from backend.db.stream_reads import ReadBounds, bounded_connection
from backend.domain.streaming import CueCoverage


def settings_in(options: str) -> dict[str, str]:
    """``-c name=value`` startup options as a dict (order does not matter)."""
    parts = options.split()
    assert parts[::2] == ["-c"] * (len(parts) // 2)
    return dict(part.split("=", 1) for part in parts[1::2])


class OneRow:
    """A connection that answers the coverage query with one row."""

    def execute(self, query: object, params: object = None) -> OneRow:
        return self

    def fetchone(self) -> dict[str, int]:
        return {"waiting": 3, "ready": 2, "failed": 1, "unhashed": 4}


def test_the_coverage_bounds_are_the_d88_numbers() -> None:
    assert (
        ReadBounds(connect_timeout_s=5, lock_timeout_ms=2_000, statement_timeout_ms=10_000)
        == COVERAGE_READ_BOUNDS
    )
    assert dependencies.COVERAGE_READ_BOUNDS is COVERAGE_READ_BOUNDS


def test_the_runs_count_builds_its_options_from_the_same_bounds() -> None:
    options = settings_in(progress_writer.coverage_options())
    assert options == settings_in(COVERAGE_READ_BOUNDS.options) | {
        "default_transaction_read_only": "on"
    }
    assert COVERAGE_READ_BOUNDS.connect_timeout_s == progress_writer.COVERAGE_CONNECT_TIMEOUT_S


def test_the_routes_count_is_read_only_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, Any]]] = []

    @contextmanager
    def connect(*args: object, **kwargs: Any) -> Iterator[OneRow]:
        calls.append((args, kwargs))
        yield OneRow()

    monkeypatch.setattr(stream_reads, "connect_sync", connect)
    repo = dependencies.get_cue_coverage()
    assert isinstance(repo, BoundedCueCoverageRepository)
    assert repo.coverage() == CueCoverage(analysable=6, ready=2, failed=1, unhashed=4)
    [(args, kwargs)] = calls
    assert kwargs["autocommit"] is True
    assert kwargs["connect_timeout"] == 5
    assert settings_in(kwargs["options"]) == {
        "lock_timeout": "2000",
        "statement_timeout": "10000",
        "default_transaction_read_only": "on",
    }
    assert stream_cue_coverage.COVERAGE_READ_BOUNDS is COVERAGE_READ_BOUNDS


def test_a_plain_bounded_connection_is_not_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # The stream service's own reads (D88) keep their options as they were.
    seen: list[str] = []

    @contextmanager
    def connect(*args: object, **kwargs: Any) -> Iterator[OneRow]:
        seen.append(kwargs["options"])
        yield OneRow()

    monkeypatch.setattr(stream_reads, "connect_sync", connect)
    with bounded_connection("postgresql://unused", COVERAGE_READ_BOUNDS):
        pass
    assert seen == [COVERAGE_READ_BOUNDS.options]
