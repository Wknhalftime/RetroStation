"""TestClient that runs the app's lifespan without leaking its logging setup."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient


@contextmanager
def lifespan_client(app: FastAPI) -> Iterator[TestClient]:
    """Enter ``TestClient(app)``, restoring structlog's configuration on exit.

    The lifespan's ``configure_logging(database_url=...)`` installs the
    ``DbLogProcessor`` globally; without the restore it outlives the client and
    turns every later structlog event on the xdist worker into an
    ``INSERT INTO system_logs``.
    """
    snapshot = structlog.get_config()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        structlog.configure(**snapshot)
