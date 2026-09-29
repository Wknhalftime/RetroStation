"""AUD-R011 characterisation tests: who owns a failed task-chain handoff.

`library_scan_task`, `library_scan_files_task` (the watcher), and
`ingestion_task` each chain into a downstream Huey task by calling it
directly (e.g. `library_enrichment_task()`), which only ENQUEUES the next
task rather than running it. Today that enqueue call is not guarded
consistently:

- `library_scan_task` and `library_scan_files_task` enqueue INSIDE their own
  try block, so an enqueue failure (patched here to raise the real
  `sqlite3.Error` SqliteHuey's storage backend raises) marks their own
  otherwise-successful run FAILED.
- Neither task writes a SystemLog at all today (started/completed/failed),
  pinned here via a global patch on `PgSystemLogRepository.__init__` so the
  assertion holds regardless of which module would go on to instantiate it.

These tests pin that behaviour on unchanged code. AUD-R011 decisions 1 and 2
change all of this; the corresponding tests are updated in the commits that
implement each decision (see the commit messages for exactly which
assertions flip and why).
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.domain.enums import TaskStatus
from backend.services.ingestion_service import IngestionResult
from backend.services.library_scan_service import FolderScanResult
from tests.fakes.system_logs import FakeSystemLogRepository
from tests.fakes.task_progress import FakeTaskProgressRepository

CSV_PAYLOAD = b"Station,Played,Artist,Title\r\nKAZR,2005-03-02 00:01:00,Artist_A,Title_A\r\n"


def _fake_connect_sync(_url: str, *, autocommit: bool = False) -> Any:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    return conn


# ---------------------------------------------------------------------------
# library_scan_task: enqueue failure ownership
# ---------------------------------------------------------------------------


class TestLibraryScanTaskEnqueueFailurePinsToday:
    @patch("backend.tasks.library_scan_tasks.connect_sync", side_effect=_fake_connect_sync)
    @patch("backend.tasks.library_scan_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_scan_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_scan_tasks._run_scan")
    def test_hash_backfill_enqueue_failure_marks_scans_own_run_failed(
        self,
        mock_run_scan: MagicMock,
        mock_sys_log_cls: MagicMock,
        mock_progress_cls: MagicMock,
        _connect: MagicMock,
    ) -> None:
        mock_run_scan.return_value = (5, 0, {"processed": 5, "total": 5, "current_path": ""})
        fake_progress = FakeTaskProgressRepository()
        mock_progress_cls.return_value = fake_progress
        mock_sys_log_cls.return_value = FakeSystemLogRepository()

        with (
            patch(
                "backend.tasks.library_hash_backfill_tasks.library_hash_backfill_task",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            patch("backend.tasks.library_enrichment_tasks.library_enrichment_task") as mock_enrich,
        ):
            from backend.tasks.library_scan_tasks import library_scan_task

            with pytest.raises(sqlite3.OperationalError):
                library_scan_task.call_local("/music")

        # Today: the enqueue failure IS the primary exception, so scan's own
        # otherwise-successful run is reported FAILED, and the second
        # enqueue (library_enrichment_task) never runs.
        mock_enrich.assert_not_called()
        terminal = fake_progress.received_upserts[-1]
        assert terminal.status == TaskStatus.FAILED
        assert terminal.progress_data["error"] == "database is locked"

    @patch("backend.tasks.library_scan_tasks.connect_sync", side_effect=_fake_connect_sync)
    @patch("backend.tasks.library_scan_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_scan_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_scan_tasks._run_scan")
    def test_enrichment_enqueue_failure_marks_scans_own_run_failed(
        self,
        mock_run_scan: MagicMock,
        mock_sys_log_cls: MagicMock,
        mock_progress_cls: MagicMock,
        _connect: MagicMock,
    ) -> None:
        mock_run_scan.return_value = (5, 0, {"processed": 5, "total": 5, "current_path": ""})
        fake_progress = FakeTaskProgressRepository()
        mock_progress_cls.return_value = fake_progress
        mock_sys_log_cls.return_value = FakeSystemLogRepository()

        with (
            patch("backend.tasks.library_hash_backfill_tasks.library_hash_backfill_task"),
            patch(
                "backend.tasks.library_enrichment_tasks.library_enrichment_task",
                side_effect=sqlite3.OperationalError("disk I/O error"),
            ),
        ):
            from backend.tasks.library_scan_tasks import library_scan_task

            with pytest.raises(sqlite3.OperationalError):
                library_scan_task.call_local("/music")

        terminal = fake_progress.received_upserts[-1]
        assert terminal.status == TaskStatus.FAILED
        assert terminal.progress_data["error"] == "disk I/O error"


class TestLibraryScanTaskFailedDetailsHaveNoTracebackYet:
    @patch("backend.tasks.library_scan_tasks.connect_sync", side_effect=_fake_connect_sync)
    @patch("backend.tasks.library_scan_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_scan_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_scan_tasks._run_scan")
    def test_scan_body_failure_failed_log_has_no_traceback(
        self,
        mock_run_scan: MagicMock,
        mock_sys_log_cls: MagicMock,
        mock_progress_cls: MagicMock,
        _connect: MagicMock,
    ) -> None:
        mock_run_scan.side_effect = RuntimeError("scan body boom")
        mock_progress_cls.return_value = FakeTaskProgressRepository()
        fake_sys_log = FakeSystemLogRepository()
        mock_sys_log_cls.return_value = fake_sys_log

        from backend.tasks.library_scan_tasks import library_scan_task

        with pytest.raises(RuntimeError, match="scan body boom"):
            library_scan_task.call_local("/music")

        failed_log = next(log for log in fake_sys_log.all if log.message == "scan_failed")
        assert failed_log.details is not None
        assert failed_log.details["error"] == "scan body boom"
        assert "traceback" not in failed_log.details


# ---------------------------------------------------------------------------
# library_scan_files_task (watcher): enqueue failure ownership
# ---------------------------------------------------------------------------


def _watcher_harness() -> tuple[MagicMock, MagicMock]:
    """Two distinct connections: progress_conn (autocommit) and library_conn.

    library_conn.execute(...) must support the advisory-lock SELECT: its
    result's `.fetchone()` returns a mapping with `acquired=True` so the
    lock-held early-return branch is not taken.
    """
    progress_conn = MagicMock()
    library_conn = MagicMock()
    lock_result = MagicMock()
    lock_result.fetchone.return_value = {"acquired": True}
    library_conn.execute.return_value = lock_result
    return progress_conn, library_conn


class TestWatcherScanTaskEnqueueFailurePinsToday:
    @patch("backend.tasks.library_scan_tasks.reconcile_missing_after_scan")
    @patch("backend.tasks.library_watcher_tasks.scan_folder_incrementally")
    @patch("backend.tasks.library_watcher_tasks.RepositoryFactory")
    @patch("backend.tasks.library_watcher_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_watcher_tasks.connect_sync")
    def test_enrichment_enqueue_failure_marks_watchers_own_run_failed(
        self,
        mock_connect: MagicMock,
        mock_progress_cls: MagicMock,
        mock_repo_factory: MagicMock,
        mock_scan_folder: MagicMock,
        _mock_reconcile: MagicMock,
    ) -> None:
        progress_conn, library_conn = _watcher_harness()
        mock_connect.side_effect = [progress_conn, library_conn]
        mock_repo_factory.return_value.library_files.get_by_folder_path.return_value = []
        mock_scan_folder.return_value = FolderScanResult(files_written=1)
        fake_progress = FakeTaskProgressRepository()
        mock_progress_cls.return_value = fake_progress

        with patch(
            "backend.tasks.library_enrichment_tasks.library_enrichment_task",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            from backend.tasks.library_watcher_tasks import library_scan_files_task

            with pytest.raises(sqlite3.OperationalError):
                library_scan_files_task.call_local(["/music/jazz"], uuid4().hex)

        terminal = fake_progress.received_upserts[-1]
        assert terminal.status == TaskStatus.FAILED
        assert terminal.progress_data["error"] == "database is locked"


# ---------------------------------------------------------------------------
# Neither the watcher nor ingestion write any SystemLog today, on success or
# on failure. Pinned via a patch on the concrete repository class itself so
# the assertion holds regardless of which module ends up instantiating it.
# ---------------------------------------------------------------------------


class TestWatcherAndIngestionWriteNoSystemLogYet:
    @patch("backend.tasks.library_scan_tasks.reconcile_missing_after_scan")
    @patch("backend.tasks.library_watcher_tasks.scan_folder_incrementally")
    @patch("backend.tasks.library_watcher_tasks.RepositoryFactory")
    @patch("backend.tasks.library_watcher_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_watcher_tasks.connect_sync")
    def test_watcher_success_writes_no_system_log(
        self,
        mock_connect: MagicMock,
        mock_progress_cls: MagicMock,
        mock_repo_factory: MagicMock,
        mock_scan_folder: MagicMock,
        _mock_reconcile: MagicMock,
    ) -> None:
        progress_conn, library_conn = _watcher_harness()
        mock_connect.side_effect = [progress_conn, library_conn]
        mock_repo_factory.return_value.library_files.get_by_folder_path.return_value = []
        mock_scan_folder.return_value = FolderScanResult(files_written=0)
        mock_progress_cls.return_value = FakeTaskProgressRepository()

        with (
            patch.object(PgSystemLogRepository, "__init__", return_value=None) as ctor,
            patch("backend.tasks.library_enrichment_tasks.library_enrichment_task"),
        ):
            from backend.tasks.library_watcher_tasks import library_scan_files_task

            library_scan_files_task.call_local(["/music/jazz"], uuid4().hex)

        ctor.assert_not_called()

    @patch("backend.tasks.library_scan_tasks.reconcile_missing_after_scan")
    @patch("backend.tasks.library_watcher_tasks.scan_folder_incrementally")
    @patch("backend.tasks.library_watcher_tasks.RepositoryFactory")
    @patch("backend.tasks.library_watcher_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_watcher_tasks.connect_sync")
    def test_watcher_failure_writes_no_system_log(
        self,
        mock_connect: MagicMock,
        mock_progress_cls: MagicMock,
        mock_repo_factory: MagicMock,
        mock_scan_folder: MagicMock,
        _mock_reconcile: MagicMock,
    ) -> None:
        progress_conn, library_conn = _watcher_harness()
        mock_connect.side_effect = [progress_conn, library_conn]
        mock_scan_folder.side_effect = RuntimeError("folder scan boom")
        mock_progress_cls.return_value = FakeTaskProgressRepository()

        with patch.object(PgSystemLogRepository, "__init__", return_value=None) as ctor:
            from backend.tasks.library_watcher_tasks import library_scan_files_task

            with pytest.raises(RuntimeError, match="folder scan boom"):
                library_scan_files_task.call_local(["/music/jazz"], uuid4().hex)

        ctor.assert_not_called()

    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=1)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_ingestion_success_writes_no_system_log(
        self,
        _connect: MagicMock,
        mock_progress_cls: MagicMock,
        mock_run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        mock_progress_cls.return_value = FakeTaskProgressRepository()
        mock_run_ingest.return_value = IngestionResult(
            playlist_id="pl-1",
            rows_processed=1,
            rows_skipped=0,
            artists_created=1,
            identities_created=1,
            events_created=1,
            broadcast_days_created=1,
        )

        with (
            patch.object(PgSystemLogRepository, "__init__", return_value=None) as ctor,
            patch("backend.tasks.embedding_tasks.embedding_task"),
        ):
            from backend.tasks.ingestion_tasks import ingestion_task

            ingestion_task.call_local(CSV_PAYLOAD, "f.csv", str(uuid4()), "tid-1")

        ctor.assert_not_called()

    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=1)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_ingestion_failure_writes_no_system_log(
        self,
        _connect: MagicMock,
        mock_progress_cls: MagicMock,
        mock_run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        mock_progress_cls.return_value = FakeTaskProgressRepository()
        mock_run_ingest.side_effect = RuntimeError("ingest boom")

        with patch.object(PgSystemLogRepository, "__init__", return_value=None) as ctor:
            from backend.tasks.ingestion_tasks import ingestion_task

            with pytest.raises(RuntimeError, match="ingest boom"):
                ingestion_task.call_local(CSV_PAYLOAD, "f.csv", str(uuid4()), "tid-2")

        ctor.assert_not_called()


# ---------------------------------------------------------------------------
# ingestion_task: embedding_task enqueue failure is silently swallowed today
# (contextlib.suppress(Exception) around the whole call). Already pinned by
# tests/tasks/test_ingestion_task_progress.py::
# TestIngestionTaskEmbeddingDecoupling::test_embedding_failure_does_not_flip_to_failed
# (updated in the decision-1 commit to use the real enqueue error type and
# assert the new SystemLog).
# ---------------------------------------------------------------------------
