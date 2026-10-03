"""The post-commit master recalc still swallows a lost connection once its repositories
translate it (follow-up to PR #133).

Requirements: ``identity_resolution_service`` module docstring (a database failure in the
recalc, "the connection dropping" included, is caught and logged so the committed manual match
is never undone). The library-file and song-master repositories now raise their subdomain's
``StorageUnavailableError`` for a lost connection instead of a ``psycopg.Error``, so the recalc
must catch that too. The backend is terminated with ``pg_terminate_backend`` right after the
recalc's connection opens.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

import backend.db.sync_conn as sync_conn
from backend.services import identity_resolution_service
from backend.services.repository_factory import recalc_repos

pytestmark = pytest.mark.integration


def test_a_lost_connection_in_the_recalc_is_logged_and_not_raised(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real: Callable[..., psycopg.Connection[Any]] = sync_conn.connect_sync

    def connect(dsn: str, **kwargs: Any) -> psycopg.Connection[Any]:
        conn = real(dsn, **kwargs)
        with psycopg.connect(migrated_db, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))
        return conn

    # recalc_repos holds its own reference to connect_sync.
    monkeypatch.setattr("backend.services.repository_factory.connect_sync", connect)

    with capture_logs() as events:
        identity_resolution_service.recalculate_for_work_sync(
            migrated_db, "some-work-id", recalc_repos
        )

    warnings = [e for e in events if e.get("event") == "manual_resolve_recalc_failed_inner"]
    assert len(warnings) == 1
