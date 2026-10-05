"""AUD-R012 characterisation tests: who owns a failed task-chain handoff.

`library_scan_task`, `library_scan_files_task` (the watcher), and
`ingestion_task` each chain into a downstream Huey task by calling it
directly (e.g. `library_enrichment_task()`), which only ENQUEUES the next
task rather than running it.

AUD-R012 (1): the caller owns the handoff. An enqueue failure
(the real `sqlite3.Error` SqliteHuey's storage backend raises) is logged as
an ERROR SystemLog on the caller's own trace_id via
`backend.tasks._enqueue_chain.enqueue_or_log`, and the caller keeps its own
COMPLETED status — it no longer flips to FAILED. `library_scan_task`
enqueues two tasks; if the first fails, the second is still attempted, and
each failure is logged on its own.

AUD-R012 (2): the watcher and `ingestion_task` write their own
started/completed/failed SystemLogs (`TestWatcherLifecycleLogs`,
`TestIngestionLifecycleLogs`). AUD-R012 (3): `library_scan_task`'s failed
SystemLog carries a traceback.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from backend.domain.enums import LogCategory, LogLevel, TaskStatus
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
# library_scan_task: enqueue failure ownership (AUD-R012 (1))
# ---------------------------------------------------------------------------


class TestLibraryScanTaskEnqueueFailureOwnership:
    @patch("backend.tasks.library_scan_tasks.connect_sync", side_effect=_fake_connect_sync)
    @patch("backend.tasks.library_scan_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_scan_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_scan_tasks._run_scan")
    def test_hash_backfill_enqueue_failure_logs_and_keeps_scan_completed(
        self,
        mock_run_scan: MagicMock,
        mock_sys_log_cls: MagicMock,
        mock_progress_cls: MagicMock,
        _connect: MagicMock,
    ) -> None:
        mock_run_scan.return_value = (5, 0, {"processed": 5, "total": 5, "current_path": ""})
        fake_progress = FakeTaskProgressRepository()
        mock_progress_cls.return_value = fake_progress
        fake_sys_log = FakeSystemLogRepository()
        mock_sys_log_cls.return_value = fake_sys_log

        with (
            patch(
                "backend.tasks.library_hash_backfill_tasks.library_hash_backfill_task",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            patch("backend.tasks.library_enrichment_tasks.library_enrichment_task") as mock_enrich,
        ):
            from backend.tasks.library_scan_tasks import library_scan_task

            # Must NOT raise: the caller owns the handoff and keeps COMPLETED.
            library_scan_task.call_local("/music")

        # The second enqueue is still attempted even though the first failed.
        mock_enrich.assert_called_once()
        terminal = fake_progress.received_upserts[-1]
        assert terminal.status == TaskStatus.COMPLETED

        enqueue_failed_logs = [
            log
            for log in fake_sys_log.all
            if log.message == "library_hash_backfill_task_enqueue_failed"
        ]
        assert len(enqueue_failed_logs) == 1
        assert enqueue_failed_logs[0].trace_id == terminal.task_id
        assert enqueue_failed_logs[0].details is not None
        assert enqueue_failed_logs[0].details["error"] == "database is locked"

    @patch("backend.tasks.library_scan_tasks.connect_sync", side_effect=_fake_connect_sync)
    @patch("backend.tasks.library_scan_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_scan_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_scan_tasks._run_scan")
    def test_enrichment_enqueue_failure_logs_and_keeps_scan_completed(
        self,
        mock_run_scan: MagicMock,
        mock_sys_log_cls: MagicMock,
        mock_progress_cls: MagicMock,
        _connect: MagicMock,
    ) -> None:
        mock_run_scan.return_value = (5, 0, {"processed": 5, "total": 5, "current_path": ""})
        fake_progress = FakeTaskProgressRepository()
        mock_progress_cls.return_value = fake_progress
        fake_sys_log = FakeSystemLogRepository()
        mock_sys_log_cls.return_value = fake_sys_log

        with (
            patch("backend.tasks.library_hash_backfill_tasks.library_hash_backfill_task"),
            patch(
                "backend.tasks.library_enrichment_tasks.library_enrichment_task",
                side_effect=sqlite3.OperationalError("disk I/O error"),
            ),
        ):
            from backend.tasks.library_scan_tasks import library_scan_task

            library_scan_task.call_local("/music")

        terminal = fake_progress.received_upserts[-1]
        assert terminal.status == TaskStatus.COMPLETED

        enqueue_failed_logs = [
            log
            for log in fake_sys_log.all
            if log.message == "library_enrichment_task_enqueue_failed"
        ]
        assert len(enqueue_failed_logs) == 1
        assert enqueue_failed_logs[0].details is not None
        assert enqueue_failed_logs[0].details["error"] == "disk I/O error"

    @patch("backend.tasks.library_scan_tasks.connect_sync", side_effect=_fake_connect_sync)
    @patch("backend.tasks.library_scan_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_scan_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_scan_tasks._run_scan")
    def test_both_enqueue_failures_are_logged_separately(
        self,
        mock_run_scan: MagicMock,
        mock_sys_log_cls: MagicMock,
        mock_progress_cls: MagicMock,
        _connect: MagicMock,
    ) -> None:
        mock_run_scan.return_value = (5, 0, {"processed": 5, "total": 5, "current_path": ""})
        mock_progress_cls.return_value = FakeTaskProgressRepository()
        fake_sys_log = FakeSystemLogRepository()
        mock_sys_log_cls.return_value = fake_sys_log

        with (
            patch(
                "backend.tasks.library_hash_backfill_tasks.library_hash_backfill_task",
                side_effect=sqlite3.OperationalError("first failure"),
            ),
            patch(
                "backend.tasks.library_enrichment_tasks.library_enrichment_task",
                side_effect=sqlite3.OperationalError("second failure"),
            ),
        ):
            from backend.tasks.library_scan_tasks import library_scan_task

            library_scan_task.call_local("/music")

        messages = {log.message: log.details for log in fake_sys_log.all}
        assert messages["library_hash_backfill_task_enqueue_failed"]["error"] == "first failure"
        assert messages["library_enrichment_task_enqueue_failed"]["error"] == "second failure"


class TestLibraryScanTaskFailedDetailsHaveTraceback:
    """AUD-R012 (3): library_scan_task's failed SystemLog details now
    carry a traceback, matching `_task_run.task_run`'s existing shape.
    """

    @patch("backend.tasks.library_scan_tasks.connect_sync", side_effect=_fake_connect_sync)
    @patch("backend.tasks.library_scan_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_scan_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_scan_tasks._run_scan")
    def test_scan_body_failure_failed_log_has_traceback(
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
        assert "RuntimeError: scan body boom" in failed_log.details["traceback"]


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


class TestWatcherScanTaskEnqueueFailureOwnership:
    @patch("backend.tasks.library_scan_tasks.reconcile_missing_after_scan")
    @patch("backend.tasks.library_watcher_tasks.scan_folder_incrementally")
    @patch("backend.tasks.library_watcher_tasks.RepositoryFactory")
    @patch("backend.tasks.library_watcher_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_watcher_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_watcher_tasks.connect_sync")
    def test_enrichment_enqueue_failure_logs_and_keeps_watcher_completed(
        self,
        mock_connect: MagicMock,
        mock_sys_log_cls: MagicMock,
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
        fake_sys_log = FakeSystemLogRepository()
        mock_sys_log_cls.return_value = fake_sys_log

        with patch(
            "backend.tasks.library_enrichment_tasks.library_enrichment_task",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            from backend.tasks.library_watcher_tasks import library_scan_files_task

            # Must NOT raise: the caller owns the handoff and keeps COMPLETED.
            library_scan_files_task.call_local(["/music/jazz"], uuid4().hex)

        terminal = fake_progress.received_upserts[-1]
        assert terminal.status == TaskStatus.COMPLETED

        enqueue_failed_logs = [
            log
            for log in fake_sys_log.all
            if log.message == "library_enrichment_task_enqueue_failed"
        ]
        assert len(enqueue_failed_logs) == 1
        assert enqueue_failed_logs[0].trace_id == terminal.task_id
        assert enqueue_failed_logs[0].details is not None
        assert enqueue_failed_logs[0].details["error"] == "database is locked"


# ---------------------------------------------------------------------------
# AUD-R012 (2): the watcher and ingestion_task now write their own
# started/completed/failed SystemLogs, like their peers (library_scan_task,
# library_enrichment_task, mb_enrichment_task).
# ---------------------------------------------------------------------------


class TestWatcherLifecycleLogs:
    @patch("backend.tasks.library_scan_tasks.reconcile_missing_after_scan")
    @patch("backend.tasks.library_watcher_tasks.scan_folder_incrementally")
    @patch("backend.tasks.library_watcher_tasks.RepositoryFactory")
    @patch("backend.tasks.library_watcher_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_watcher_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_watcher_tasks.connect_sync")
    def test_watcher_success_writes_started_and_completed(
        self,
        mock_connect: MagicMock,
        mock_sys_log_cls: MagicMock,
        mock_progress_cls: MagicMock,
        mock_repo_factory: MagicMock,
        mock_scan_folder: MagicMock,
        _mock_reconcile: MagicMock,
    ) -> None:
        progress_conn, library_conn = _watcher_harness()
        mock_connect.side_effect = [progress_conn, library_conn]
        mock_repo_factory.return_value.library_files.get_by_folder_path.return_value = []
        mock_scan_folder.return_value = FolderScanResult(files_written=3)
        mock_progress_cls.return_value = FakeTaskProgressRepository()
        fake_sys_log = FakeSystemLogRepository()
        mock_sys_log_cls.return_value = fake_sys_log

        with patch("backend.tasks.library_enrichment_tasks.library_enrichment_task"):
            from backend.tasks.library_watcher_tasks import library_scan_files_task

            library_scan_files_task.call_local(["/music/jazz"], uuid4().hex)

        messages = [log.message for log in fake_sys_log.all]
        assert messages == ["watcher_scan_started", "watcher_scan_completed"]

        started = fake_sys_log.all[0]
        assert started.level == LogLevel.INFO
        assert started.category == LogCategory.SCAN
        assert started.details == {"folder_count": 1}

        completed = fake_sys_log.all[1]
        assert completed.level == LogLevel.INFO
        assert completed.trace_id == started.trace_id
        assert completed.details == {"processed": 1, "total": 1, "files_written": 3}

    @patch("backend.tasks.library_scan_tasks.reconcile_missing_after_scan")
    @patch("backend.tasks.library_watcher_tasks.scan_folder_incrementally")
    @patch("backend.tasks.library_watcher_tasks.RepositoryFactory")
    @patch("backend.tasks.library_watcher_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_watcher_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_watcher_tasks.connect_sync")
    def test_watcher_body_failure_writes_started_and_failed_with_traceback(
        self,
        mock_connect: MagicMock,
        mock_sys_log_cls: MagicMock,
        mock_progress_cls: MagicMock,
        mock_repo_factory: MagicMock,
        mock_scan_folder: MagicMock,
        _mock_reconcile: MagicMock,
    ) -> None:
        progress_conn, library_conn = _watcher_harness()
        mock_connect.side_effect = [progress_conn, library_conn]
        mock_scan_folder.side_effect = RuntimeError("folder scan boom")
        mock_progress_cls.return_value = FakeTaskProgressRepository()
        fake_sys_log = FakeSystemLogRepository()
        mock_sys_log_cls.return_value = fake_sys_log

        from backend.tasks.library_watcher_tasks import library_scan_files_task

        with pytest.raises(RuntimeError, match="folder scan boom"):
            library_scan_files_task.call_local(["/music/jazz"], uuid4().hex)

        messages = [log.message for log in fake_sys_log.all]
        assert messages == ["watcher_scan_started", "watcher_scan_failed"]

        failed = fake_sys_log.all[1]
        assert failed.level == LogLevel.ERROR
        assert failed.category == LogCategory.SCAN
        assert failed.details is not None
        assert failed.details["error"] == "folder scan boom"
        assert failed.details["traceback"].strip() != ""
        assert "RuntimeError: folder scan boom" in failed.details["traceback"]


class TestIngestionLifecycleLogs:
    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=1)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.PgSystemLogRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_ingestion_success_writes_started_and_completed(
        self,
        _connect: MagicMock,
        mock_sys_log_cls: MagicMock,
        mock_progress_cls: MagicMock,
        mock_run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        mock_progress_cls.return_value = FakeTaskProgressRepository()
        fake_sys_log = FakeSystemLogRepository()
        mock_sys_log_cls.return_value = fake_sys_log
        mock_run_ingest.return_value = IngestionResult(
            playlist_id="pl-1",
            rows_processed=1,
            rows_skipped=0,
            artists_created=1,
            identities_created=1,
            events_created=1,
            broadcast_days_created=1,
        )

        with patch("backend.tasks.embedding_tasks.embedding_task"):
            from backend.tasks.ingestion_tasks import ingestion_task

            ingestion_task.call_local(CSV_PAYLOAD, "f.csv", str(uuid4()), "tid-1")

        messages = [log.message for log in fake_sys_log.all]
        assert messages == ["ingestion_started", "ingestion_completed"]

        started = fake_sys_log.all[0]
        assert started.level == LogLevel.INFO
        assert started.category == LogCategory.INGESTION
        assert started.trace_id == "tid-1"
        assert started.details == {"filename": "f.csv", "total_rows": 1}

        completed = fake_sys_log.all[1]
        assert completed.level == LogLevel.INFO
        assert completed.details is not None
        assert completed.details["playlist_id"] == "pl-1"
        assert completed.details["processed"] == 1

    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=1)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.PgSystemLogRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_ingestion_body_failure_writes_started_and_failed_with_traceback(
        self,
        _connect: MagicMock,
        mock_sys_log_cls: MagicMock,
        mock_progress_cls: MagicMock,
        mock_run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        mock_progress_cls.return_value = FakeTaskProgressRepository()
        fake_sys_log = FakeSystemLogRepository()
        mock_sys_log_cls.return_value = fake_sys_log
        mock_run_ingest.side_effect = RuntimeError("ingest boom")

        from backend.tasks.ingestion_tasks import ingestion_task

        with pytest.raises(RuntimeError, match="ingest boom"):
            ingestion_task.call_local(CSV_PAYLOAD, "f.csv", str(uuid4()), "tid-2")

        messages = [log.message for log in fake_sys_log.all]
        assert messages == ["ingestion_started", "ingestion_failed"]

        failed = fake_sys_log.all[1]
        assert failed.level == LogLevel.ERROR
        assert failed.category == LogCategory.INGESTION
        assert failed.trace_id == "tid-2"
        assert failed.details is not None
        assert failed.details["error"] == "ingest boom"
        assert "RuntimeError: ingest boom" in failed.details["traceback"]

    @patch("backend.tasks.ingestion_tasks.logger")
    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=1)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.PgSystemLogRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_transient_systemlog_write_fault_is_dropped_not_fatal(
        self,
        _connect: MagicMock,
        mock_sys_log_cls: MagicMock,
        mock_progress_cls: MagicMock,
        mock_run_ingest: MagicMock,
        _count: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        """`_safe_system_log_create`'s own guard: a transient driver fault
        writing the "started" log must not abort ingestion — it is dropped
        and warned about, mirroring `_safe_progress_upsert`.
        """
        import psycopg

        class _DroppedStartedLogRepo(FakeSystemLogRepository):
            def create(self, log: Any) -> None:
                if log.message == "ingestion_started":
                    raise psycopg.OperationalError("progress conn dropped")
                super().create(log)

        fake_repo = FakeTaskProgressRepository()
        mock_progress_cls.return_value = fake_repo
        fake_sys_log = _DroppedStartedLogRepo()
        mock_sys_log_cls.return_value = fake_sys_log
        mock_run_ingest.return_value = IngestionResult(
            playlist_id="pl-1",
            rows_processed=1,
            rows_skipped=0,
            artists_created=1,
            identities_created=1,
            events_created=1,
            broadcast_days_created=1,
        )

        with patch("backend.tasks.embedding_tasks.embedding_task"):
            from backend.tasks.ingestion_tasks import ingestion_task

            ingestion_task.call_local(CSV_PAYLOAD, "f.csv", str(uuid4()), "tid-3")

        # Only "ingestion_completed" made it through; "ingestion_started" was
        # dropped, not raised.
        assert [log.message for log in fake_sys_log.all] == ["ingestion_completed"]
        assert fake_repo.received_upserts[-1].status == TaskStatus.COMPLETED

        dropped_warnings = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and c.args[0] == "ingestion_system_log_create_dropped"
        ]
        assert len(dropped_warnings) == 1
        assert dropped_warnings[0].kwargs["lifecycle"] == "started"

    @patch("backend.tasks.ingestion_tasks.logger")
    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=1)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.PgSystemLogRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_non_transient_systemlog_write_fault_on_failed_log_does_not_shadow_original(
        self,
        _connect: MagicMock,
        mock_sys_log_cls: MagicMock,
        mock_progress_cls: MagicMock,
        mock_run_ingest: MagicMock,
        _count: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        """A non-transient bug (outside `_PROGRESS_DROP_ERRORS`) writing the
        "failed" SystemLog is caught by the caller's own shadow guard — the
        real ingestion exception must still be what propagates, not this
        secondary systemlog-write bug.
        """

        class _ShadowFailingLogRepo(FakeSystemLogRepository):
            def create(self, log: Any) -> None:
                if log.message == "ingestion_failed":
                    raise ValueError("system_logs schema drift")
                super().create(log)

        mock_progress_cls.return_value = FakeTaskProgressRepository()
        mock_sys_log_cls.return_value = _ShadowFailingLogRepo()
        mock_run_ingest.side_effect = RuntimeError("real ingestion boom")

        from backend.tasks.ingestion_tasks import ingestion_task

        with pytest.raises(RuntimeError, match="real ingestion boom"):
            ingestion_task.call_local(CSV_PAYLOAD, "f.csv", str(uuid4()), "tid-4")

        shadow_warnings = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and c.args[0] == "ingestion_system_log_create_shadow_prevented"
        ]
        assert len(shadow_warnings) == 1
        assert shadow_warnings[0].kwargs["original_error"] == "real ingestion boom"
        assert shadow_warnings[0].kwargs["shadow_error"] == "system_logs schema drift"


# ---------------------------------------------------------------------------
# ingestion_task: embedding_task enqueue failure ownership is covered in
# tests/tasks/test_ingestion_task_progress.py::TestIngestionTaskEmbeddingDecoupling
# (kept there, next to the rest of ingestion_task's lifecycle tests).
# ---------------------------------------------------------------------------
