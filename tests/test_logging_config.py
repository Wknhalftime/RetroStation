"""Logging must never turn into an exception inside the code that logs.

On Windows a worker whose parent (honcho) has died keeps running with a
stdout pipe nobody reads; every write then raises ``OSError: [Errno 22]
Invalid argument``. On 2026-09-25 that surfaced as ``mb_enrichment_task``
failing with that message and no traceback.
"""
from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from backend.logging_config import configure_logging


class _DeadPipe:
    """A stdout whose reader has gone away, as Windows reports it."""

    encoding = "utf-8"

    def write(self, _s: str) -> int:
        raise OSError(22, "Invalid argument")

    def flush(self) -> None:
        raise OSError(22, "Invalid argument")

    def reconfigure(self, **_kwargs: Any) -> None:
        return None


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    original = structlog.get_config()
    try:
        yield
    finally:
        structlog.configure(**original)


@pytest.mark.usefixtures("_restore_logging")
def test_log_call_survives_a_dead_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_logging("INFO")
    monkeypatch.setattr(sys, "stdout", _DeadPipe())

    structlog.get_logger().info("worker_still_working", item=1)
