"""The after-scan purge never fails the scan it follows, not even on reading its setting."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import psycopg

from backend.tasks.library_scan_tasks import purge_missing_after_scan


def test_a_setting_read_the_database_refuses_skips_the_purge() -> None:
    conn, repos = MagicMock(), MagicMock()
    repos.user_settings.get.side_effect = psycopg.errors.OperationalError("simulated")

    result = purge_missing_after_scan(conn, repos, Path("/lib"), walk_saw_files=True)

    assert result is None
    conn.rollback.assert_called_once_with()
    conn.commit.assert_not_called()


def test_an_empty_walk_reads_no_setting() -> None:
    conn, repos = MagicMock(), MagicMock()

    assert purge_missing_after_scan(conn, repos, Path("/lib"), walk_saw_files=False) is None
    repos.user_settings.get.assert_not_called()
    conn.commit.assert_not_called()
