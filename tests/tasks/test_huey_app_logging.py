"""The Huey consumer logs to ``system_logs``; importing the task modules must not.

``huey_app`` used to call ``configure_logging(database_url=...)`` at import time,
so any test that imported a task module left structlog's ``DbLogProcessor``
installed for the rest of its xdist worker: every later warning in unrelated code
became an ``INSERT INTO system_logs`` on the sink's own connection.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator

import pytest
import structlog
from huey.consumer import Worker  # type: ignore[import-untyped]

from backend.config import get_settings
from backend.logging_config import DbLogProcessor

# Run in a fresh interpreter: this xdist worker may have imported huey_app already.
_IMPORT_PROBE = """
import structlog
import backend.tasks.huey_app
from backend.logging_config import DbLogProcessor
chain = structlog.get_config()["processors"]
print(sum(isinstance(p, DbLogProcessor) for p in chain))
"""


def _db_sinks() -> list[DbLogProcessor]:
    return [p for p in structlog.get_config()["processors"] if isinstance(p, DbLogProcessor)]


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    original = structlog.get_config()
    try:
        yield
    finally:
        structlog.configure(**original)


@pytest.fixture
def _fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://probe@localhost:5432/never_connected")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_importing_huey_app_installs_no_db_sink() -> None:
    probe = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip().splitlines()[-1] == "0"


@pytest.mark.usefixtures("_restore_logging", "_fresh_settings")
def test_consumer_worker_startup_installs_one_db_sink() -> None:
    from backend.tasks.huey_app import huey

    structlog.reset_defaults()

    Worker(huey, default_delay=0.1, max_delay=10.0, backoff=1.15).initialize()

    assert len(_db_sinks()) == 1
