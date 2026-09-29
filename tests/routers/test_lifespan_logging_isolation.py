"""Entering the app's lifespan in a test must not leave the DB log sink behind.

``backend.main.lifespan`` calls ``configure_logging(database_url=...)``, which
installs structlog's ``DbLogProcessor``. A TestClient fixture that never restored
structlog left that sink live for the rest of its xdist worker: every later
structlog event in unrelated code became an ``INSERT INTO system_logs`` on the
sink's own connection.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog

from backend.logging_config import DbLogProcessor
from tests.lifespan_client import lifespan_client


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
def _app_settings(_migrated_db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from backend.config import get_settings

    monkeypatch.setenv("DATABASE_URL", _migrated_db_url)
    monkeypatch.setenv("RETROSTATION_SKIP_BOOT_MIGRATIONS", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("_restore_logging", "_app_settings")
def test_exiting_app_client_leaves_no_db_sink() -> None:
    from backend.main import app

    structlog.reset_defaults()

    with lifespan_client(app) as client:
        assert len(_db_sinks()) == 1, "lifespan should install the sink while the app runs"
        client.get("/health")

    assert _db_sinks() == []
