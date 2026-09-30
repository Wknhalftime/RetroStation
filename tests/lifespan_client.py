"""TestClient that runs the app's lifespan without leaking its logging setup."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import get_settings


@contextmanager
def lifespan_client(app: FastAPI) -> Iterator[TestClient]:
    """Enter ``TestClient(app)``, restoring structlog's configuration on exit.

    The lifespan's ``configure_logging(database_url=...)`` installs the
    ``DbLogProcessor`` globally; without the restore it outlives the client and
    turns every later structlog event on the xdist worker into an
    ``INSERT INTO system_logs``.

    Streaming is forced off (``STREAM_ENABLED=false``, review I5), so a lifespan under
    test never warms Liquidsoap's cache or prunes real engine logs, whatever ``.env``
    says. The variable and ``get_settings``' cache are restored on exit.
    """
    snapshot = structlog.get_config()
    previous = os.environ.get("STREAM_ENABLED")
    os.environ["STREAM_ENABLED"] = "false"
    get_settings.cache_clear()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        structlog.configure(**snapshot)
        if previous is None:
            os.environ.pop("STREAM_ENABLED", None)
        else:
            os.environ["STREAM_ENABLED"] = previous
        get_settings.cache_clear()
