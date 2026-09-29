"""Unit tests for `backend.tasks._enqueue_chain.enqueue_or_log`."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

from backend.domain.enums import LogCategory
from backend.tasks._enqueue_chain import enqueue_or_log
from tests.fakes.system_logs import FakeSystemLogRepository


class TestEnqueueOrLog:
    def test_success_does_not_write_a_log(self) -> None:
        enqueue = MagicMock()
        sys_log_repo = FakeSystemLogRepository()

        enqueue_or_log(
            enqueue,
            task_name="next_task",
            caller_task_id="caller-1",
            log_category=LogCategory.SCAN,
            sys_log_repo=sys_log_repo,
        )

        enqueue.assert_called_once()
        assert sys_log_repo.all == []

    def test_enqueue_failure_is_logged_and_swallowed(self) -> None:
        enqueue = MagicMock(side_effect=sqlite3.OperationalError("database is locked"))
        sys_log_repo = FakeSystemLogRepository()

        enqueue_or_log(
            enqueue,
            task_name="next_task",
            caller_task_id="caller-1",
            log_category=LogCategory.SCAN,
            sys_log_repo=sys_log_repo,
        )

        assert len(sys_log_repo.all) == 1
        log = sys_log_repo.all[0]
        assert log.message == "next_task_enqueue_failed"
        assert log.trace_id == "caller-1"
        assert log.category == LogCategory.SCAN
        assert log.details is not None
        assert log.details["error"] == "database is locked"
        assert "traceback" in log.details

    def test_non_enqueue_error_propagates_unguarded(self) -> None:
        enqueue = MagicMock(side_effect=RuntimeError("logic bug"))
        sys_log_repo = FakeSystemLogRepository()

        try:
            enqueue_or_log(
                enqueue,
                task_name="next_task",
                caller_task_id="caller-1",
                log_category=LogCategory.SCAN,
                sys_log_repo=sys_log_repo,
            )
        except RuntimeError as exc:
            assert str(exc) == "logic bug"
        else:
            raise AssertionError("expected RuntimeError to propagate")

        assert sys_log_repo.all == []

    @patch("backend.tasks._enqueue_chain.logger")
    def test_systemlog_write_failure_is_itself_swallowed_and_warned(
        self, mock_logger: MagicMock
    ) -> None:
        """If the enqueue AND the failure-log write both raise, the helper
        must still not propagate — a telemetry fault must never surface in
        place of, or on top of, the enqueue failure it is trying to report.
        """
        enqueue = MagicMock(side_effect=sqlite3.OperationalError("database is locked"))
        sys_log_repo = MagicMock()
        sys_log_repo.create.side_effect = RuntimeError("system_logs table is gone")

        enqueue_or_log(
            enqueue,
            task_name="next_task",
            caller_task_id="caller-1",
            log_category=LogCategory.SCAN,
            sys_log_repo=sys_log_repo,
        )

        mock_logger.warning.assert_called_once()
        args, kwargs = mock_logger.warning.call_args
        assert args[0] == "enqueue_failure_systemlog_write_failed"
        assert kwargs["caller_task_id"] == "caller-1"
        assert kwargs["next_task"] == "next_task"
        assert kwargs["enqueue_error"] == "database is locked"
        assert kwargs["telemetry_error"] == "system_logs table is gone"
