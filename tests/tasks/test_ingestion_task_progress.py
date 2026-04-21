"""Progress-lifecycle tests for ingestion_task.

The task writes `TaskProgress` rows with status transitions
RUNNING → COMPLETED (or FAILED) on a separate autocommit connection
so the WebSocket poller can observe progress while the ingest
transaction is still open.

All tests use ``FakeTaskProgressRepository`` per project rule
"In-memory fakes in tests/fakes/ implement the same ABCs".
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
import pytest

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.services.ingestion_service import (
    CsvDecodeError,
    DuplicatePlaylistError,
    IngestionResult,
)
from tests.fakes.task_progress import FakeTaskProgressRepository

CSV_PAYLOAD = (
    b"Station,Played,Artist,Title\r\n"
    b"KAZR,2005-03-02 00:01:00,Artist_A,Title_A\r\n"
    b"KAZR,2005-03-02 00:02:00,Artist_B,Title_B\r\n"
)


def _fake_connect_sync(_url: str, *, autocommit: bool = False) -> Any:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    return conn


def _result(
    playlist_id: str = "pl-1", rows: int = 2, skipped: int = 0
) -> IngestionResult:
    return IngestionResult(
        playlist_id=playlist_id,
        rows_processed=rows,
        rows_skipped=skipped,
        artists_created=2,
        identities_created=2,
        events_created=rows,
        broadcast_days_created=1,
    )


class TestIngestionTaskLifecycle:
    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=2)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_writes_running_then_completed(
        self,
        _connect: MagicMock,
        repo_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        fake_repo = FakeTaskProgressRepository()
        repo_cls.return_value = fake_repo
        run_ingest.return_value = _result()

        from backend.tasks.ingestion_tasks import ingestion_task

        with patch(
            "backend.tasks.embedding_tasks.embedding_task", MagicMock()
        ):
            ingestion_task.call_local(CSV_PAYLOAD, "f.csv", str(uuid4()), "tid-1")

        statuses = [u.status for u in fake_repo.received_upserts]
        assert statuses[0] == TaskStatus.RUNNING
        assert statuses[-1] == TaskStatus.COMPLETED
        assert all(u.task_id == "tid-1" for u in fake_repo.received_upserts)
        assert all(
            u.task_type == TaskType.INGESTION for u in fake_repo.received_upserts
        )

    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=2)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_started_at_constant_and_updated_at_non_decreasing(
        self,
        _connect: MagicMock,
        repo_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        """Regression guard: `started_at` must not drift; `updated_at` must advance."""
        fake_repo = FakeTaskProgressRepository()
        repo_cls.return_value = fake_repo

        def drive_callback(*_args: Any, **kwargs: Any) -> IngestionResult:
            cb = kwargs["on_row_processed"]
            cb(100)
            cb(200)
            return _result(rows=200)

        run_ingest.side_effect = drive_callback

        from backend.tasks.ingestion_tasks import ingestion_task

        with patch(
            "backend.tasks.embedding_tasks.embedding_task", MagicMock()
        ):
            ingestion_task.call_local(CSV_PAYLOAD, "f.csv", str(uuid4()), "tid-2")

        upserts = fake_repo.received_upserts
        assert len(upserts) >= 3  # initial + at least one cadence + terminal
        first_started = upserts[0].started_at
        assert all(u.started_at == first_started for u in upserts), (
            "started_at must not drift across upserts"
        )
        updated_sequence = [u.updated_at for u in upserts]
        assert updated_sequence == sorted(updated_sequence), (
            "updated_at must be monotonically non-decreasing"
        )

    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=2)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_completed_payload_merges_ingestion_result(
        self,
        _connect: MagicMock,
        repo_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        fake_repo = FakeTaskProgressRepository()
        repo_cls.return_value = fake_repo
        run_ingest.return_value = _result(playlist_id="pl-xyz", rows=2)

        from backend.tasks.ingestion_tasks import ingestion_task

        with patch(
            "backend.tasks.embedding_tasks.embedding_task", MagicMock()
        ):
            ingestion_task.call_local(CSV_PAYLOAD, "my.csv", str(uuid4()), "tid-3")

        terminal = fake_repo.received_upserts[-1]
        assert terminal.status == TaskStatus.COMPLETED
        assert terminal.progress_data["playlist_id"] == "pl-xyz"
        assert terminal.progress_data["processed"] == 2
        assert terminal.progress_data["total"] == 2
        assert terminal.progress_data["filename"] == "my.csv"
        assert terminal.progress_data["events_created"] == 2

    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=5)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_completed_payload_surfaces_rows_skipped(
        self,
        _connect: MagicMock,
        repo_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        """Operators must see how many rows were dropped as malformed; this
        prevents silent data loss from going unnoticed."""
        fake_repo = FakeTaskProgressRepository()
        repo_cls.return_value = fake_repo
        run_ingest.return_value = _result(rows=3, skipped=2)

        from backend.tasks.ingestion_tasks import ingestion_task

        with patch(
            "backend.tasks.embedding_tasks.embedding_task", MagicMock()
        ):
            ingestion_task.call_local(
                CSV_PAYLOAD, "skipped.csv", str(uuid4()), "tid-sk"
            )

        terminal = fake_repo.received_upserts[-1]
        assert terminal.status == TaskStatus.COMPLETED
        assert terminal.progress_data["skipped"] == 2
        assert terminal.progress_data["processed"] == 3


class TestIngestionTaskFailurePaths:
    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=5)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_generic_failure_writes_failed_row_and_reraises(
        self,
        _connect: MagicMock,
        repo_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        fake_repo = FakeTaskProgressRepository()
        repo_cls.return_value = fake_repo
        run_ingest.side_effect = RuntimeError("boom")

        from backend.tasks.ingestion_tasks import ingestion_task

        with pytest.raises(RuntimeError, match="boom"):
            ingestion_task.call_local(CSV_PAYLOAD, "f.csv", str(uuid4()), "tid-f")

        terminal = fake_repo.received_upserts[-1]
        assert terminal.status == TaskStatus.FAILED
        assert terminal.progress_data["error"] == "boom"
        assert terminal.progress_data["processed"] == 0
        assert terminal.progress_data["total"] == 5
        assert "error_code" not in terminal.progress_data

    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=2)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_duplicate_playlist_sets_error_code(
        self,
        _connect: MagicMock,
        repo_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        fake_repo = FakeTaskProgressRepository()
        repo_cls.return_value = fake_repo
        run_ingest.side_effect = DuplicatePlaylistError("already ingested")

        from backend.tasks.ingestion_tasks import ingestion_task

        with pytest.raises(DuplicatePlaylistError):
            ingestion_task.call_local(CSV_PAYLOAD, "f.csv", str(uuid4()), "tid-d")

        terminal = fake_repo.received_upserts[-1]
        assert terminal.status == TaskStatus.FAILED
        assert terminal.progress_data["error_code"] == "duplicate_playlist"

    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.count_csv_rows")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_csv_decode_error_fails_before_run_ingest(
        self,
        _connect: MagicMock,
        repo_cls: MagicMock,
        count_rows: MagicMock,
        run_ingest: MagicMock,
    ) -> None:
        fake_repo = FakeTaskProgressRepository()
        repo_cls.return_value = fake_repo
        count_rows.side_effect = CsvDecodeError("bad bytes")

        from backend.tasks.ingestion_tasks import ingestion_task

        with pytest.raises(CsvDecodeError, match="bad bytes"):
            ingestion_task.call_local(
                b"garbage", "bad.csv", str(uuid4()), "tid-dec"
            )

        run_ingest.assert_not_called()
        # Only RUNNING-initial is missing because count_csv_rows runs BEFORE
        # the initial upsert in the task body. So the only upsert is FAILED.
        statuses = [u.status for u in fake_repo.received_upserts]
        assert statuses == [TaskStatus.FAILED]
        terminal = fake_repo.received_upserts[-1]
        assert terminal.progress_data["error"] == "bad bytes"
        assert terminal.progress_data["processed"] == 0
        assert terminal.progress_data["total"] == 0
        assert terminal.progress_data["filename"] == "bad.csv"


class TestIngestionTaskEmbeddingDecoupling:
    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=2)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_embedding_failure_does_not_flip_to_failed(
        self,
        _connect: MagicMock,
        repo_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        fake_repo = FakeTaskProgressRepository()
        repo_cls.return_value = fake_repo
        run_ingest.return_value = _result()

        from backend.tasks.ingestion_tasks import ingestion_task

        with patch(
            "backend.tasks.embedding_tasks.embedding_task",
            side_effect=RuntimeError("broker down"),
        ):
            # Must NOT raise: embedding dispatch is decoupled.
            ingestion_task.call_local(
                CSV_PAYLOAD, "f.csv", str(uuid4()), "tid-e"
            )

        terminal = fake_repo.received_upserts[-1]
        assert terminal.status == TaskStatus.COMPLETED


class TestIngestionTaskBackwardCompat:
    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=2)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_missing_task_id_falls_back_to_uuid(
        self,
        _connect: MagicMock,
        repo_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        """A 3-arg call (pre-progress-tracking signature that SqliteHuey may
        have persisted across a deploy) must still execute, not raise
        TypeError. The task mints its own task_id in that case."""
        fake_repo = FakeTaskProgressRepository()
        repo_cls.return_value = fake_repo
        run_ingest.return_value = _result()

        from backend.tasks.ingestion_tasks import ingestion_task

        with patch(
            "backend.tasks.embedding_tasks.embedding_task", MagicMock()
        ):
            returned_id = ingestion_task.call_local(
                CSV_PAYLOAD, "f.csv", str(uuid4())  # no task_id
            )

        assert isinstance(returned_id, str) and len(returned_id) == 32
        assert all(
            u.task_id == returned_id for u in fake_repo.received_upserts
        ), "Auto-minted task_id must be used consistently across upserts"


class TestIngestionTaskRetryReset:
    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=500)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_retry_resets_last_processed_via_on_attempt_start(
        self,
        _connect: MagicMock,
        repo_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        """After a simulated retry, FAILED payload must report 0 — not the
        stale high-water mark from the aborted prior attempt."""
        fake_repo = FakeTaskProgressRepository()
        repo_cls.return_value = fake_repo

        def simulate_retry_then_fail(*_args: Any, **kwargs: Any) -> IngestionResult:
            on_attempt = kwargs["on_attempt_start"]
            on_row = kwargs["on_row_processed"]
            # Attempt 1: climbed to 200, then failed mid-attempt.
            on_attempt()
            on_row(100)
            on_row(200)
            # Attempt 2: lock acquisition failed BEFORE ingest_csv ran, so
            # only the task-wrapper's on_attempt_start fires. No on_row.
            on_attempt()
            raise psycopg.errors.DeadlockDetected("pretend deadlock")

        run_ingest.side_effect = simulate_retry_then_fail

        from backend.tasks.ingestion_tasks import ingestion_task

        with pytest.raises(psycopg.errors.DeadlockDetected):
            ingestion_task.call_local(
                CSV_PAYLOAD, "retry.csv", str(uuid4()), "tid-r"
            )

        terminal = fake_repo.received_upserts[-1]
        assert terminal.status == TaskStatus.FAILED
        assert terminal.progress_data["processed"] == 0, (
            "on_attempt_start should have reset last_processed to 0"
        )


class _MidRunFaultRepo(FakeTaskProgressRepository):
    """Raises psycopg.OperationalError on mid-run RUNNING upserts only.

    Mid-run = status is RUNNING and progress_data["processed"] > 0. The
    initial RUNNING (processed=0) and terminal COMPLETED/FAILED upserts
    still succeed so the lifecycle assertions remain meaningful.
    """

    def upsert(self, task: TaskProgress) -> TaskProgress:  # type: ignore[override]
        if (
            task.status == TaskStatus.RUNNING
            and task.progress_data.get("processed", 0) > 0
        ):
            self.received_upserts.append(task)
            raise psycopg.OperationalError("simulated progress conn drop")
        return super().upsert(task)


class TestIngestionTaskMidRunTelemetryFault:
    @patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=200)
    @patch("backend.tasks.ingestion_tasks._run_ingest")
    @patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
    def test_progress_conn_fault_mid_run_does_not_fail_ingestion(
        self,
        _connect: MagicMock,
        repo_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        """A telemetry-only fault during on_row_processed must NOT abort
        ingestion. Without contextlib.suppress on the callback, the
        psycopg.OperationalError propagates through ingest_csv and the
        task terminates as FAILED, rolling back valid ingestion data.
        """
        fault_repo = _MidRunFaultRepo()
        repo_cls.return_value = fault_repo

        def drive_callback_twice(*_args: Any, **kwargs: Any) -> IngestionResult:
            cb = kwargs["on_row_processed"]
            cb(100)  # first fault injection
            cb(200)  # second — must still proceed
            return _result(rows=200)

        run_ingest.side_effect = drive_callback_twice

        from backend.tasks.ingestion_tasks import ingestion_task

        with patch(
            "backend.tasks.embedding_tasks.embedding_task", MagicMock()
        ):
            ingestion_task.call_local(
                CSV_PAYLOAD, "big.csv", str(uuid4()), "tid-fault"
            )

        terminal = fault_repo.received_upserts[-1]
        assert terminal.status == TaskStatus.COMPLETED, (
            "Telemetry fault must not flip ingestion to FAILED"
        )
        assert terminal.progress_data["processed"] == 200
        # Both mid-run attempts were recorded by the fault repo (as evidence
        # the callback did fire twice) even though both upserts raised.
        mid_run_attempts = [
            u
            for u in fault_repo.received_upserts
            if u.status == TaskStatus.RUNNING
            and u.progress_data.get("processed", 0) > 0
        ]
        assert len(mid_run_attempts) == 2
