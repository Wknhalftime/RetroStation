"""The after-scan purge never fails the scan it follows, and reports what it deleted."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import psycopg
from structlog.testing import capture_logs

from backend.domain.library import MissingFilePurge
from backend.tasks.library_scan_tasks import (
    PURGE_COUNT_KEYS,
    PURGE_LOG_PATH_LIMIT,
    _log_purge,
    purge_counts,
    purge_missing_after_scan,
)


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


def _purge_of(paths: int) -> MissingFilePurge:
    return MissingFilePurge(
        deleted=paths,
        matches_released=3,
        skipped=0,
        awaiting_fingerprint=1,
        still_on_disk=2,
        unreadable_folder=4,
        deleted_paths=tuple(f"/lib/{i:03}.flac" for i in range(paths)),
    )


def test_the_purge_log_names_the_first_paths_and_counts_the_rest() -> None:
    with capture_logs() as events:
        _log_purge(_purge_of(PURGE_LOG_PATH_LIMIT + 7))

    (event,) = [e for e in events if e["event"] == "missing_files_purged"]
    assert event["deleted_paths"] == [f"/lib/{i:03}.flac" for i in range(PURGE_LOG_PATH_LIMIT)]
    assert event["deleted_paths_not_logged"] == 7


def test_a_short_purge_logs_every_path() -> None:
    with capture_logs() as events:
        _log_purge(_purge_of(2))

    (event,) = [e for e in events if e["event"] == "missing_files_purged"]
    assert event["deleted_paths"] == ["/lib/000.flac", "/lib/001.flac"]
    assert event["deleted_paths_not_logged"] == 0


def test_the_purge_counts_are_reported_under_their_keys() -> None:
    counts = purge_counts(_purge_of(5))

    assert tuple(counts) == PURGE_COUNT_KEYS
    assert counts == {"missing_purged": 5, "purge_matches_released": 3, "purge_held_back": 7}
