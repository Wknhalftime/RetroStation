"""Shared lifecycle envelope for Huey tasks that maintain live TaskProgress.

Unlike `_error_boundary.task_failure_telemetry` (which only reports terminal
failure for tasks that don't otherwise track progress), this module owns the
FULL RUNNING -> COMPLETED / FAILED envelope for tasks that already write
per-item TaskProgress rows throughout their run: a dedicated autocommit
connection, the started/completed/failed SystemLog trio, and the FAILED
progress upsert on exception.

Extracted from `backend.tasks.mb_enrichment_tasks.mb_enrichment_task` and
`backend.tasks.library_enrichment_tasks.library_enrichment_task` (AUD-025),
which shared this envelope almost verbatim. Kept as a sibling of
`_error_boundary.py`, not a merge into it, because the two helpers solve
different problems: `task_failure_telemetry` is FAILED-only for tasks with
no running-state tracking; `task_run` is the full three-state envelope for
tasks that already maintain one. Forcing them into one function would need a
"do I also write RUNNING/COMPLETED" flag, which is exactly the kind of
boolean-flag-driven branching the project's design-pattern rules warn
against.
"""

from __future__ import annotations

import contextlib
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, LogLevel, TaskStatus, TaskType
from backend.domain.system import SystemLog, TaskProgress
from backend.repositories.task_progress import TaskProgressRepository

logger = structlog.get_logger()


@dataclass(frozen=True)
class TaskLifecycleMessages:
    """SystemLog `message` values for a task's three lifecycle events."""

    started: str
    completed: str
    failed: str


@dataclass(frozen=True)
class TaskRunConfig:
    """Per-task identity + failure-reporting hook for `task_run`.

    `read_progress` is called only if the wrapped block raises. It must
    return the caller's own best-known `(processed, total)` at that instant
    — e.g. a closure reading a mutable counter the task already maintains —
    so the FAILED row reports accurate partial progress without `task_run`
    tracking a shadow copy of state the caller already owns.
    """

    task_type: TaskType
    log_category: LogCategory
    messages: TaskLifecycleMessages
    read_progress: Callable[[], tuple[int, int]]


class TaskRunHandle:
    """Yielded by `task_run`. Reports RUNNING progress and SystemLog entries,
    and stages the COMPLETED payload `task_run` writes on normal exit.
    """

    def __init__(
        self,
        *,
        task_id: str,
        started_at: datetime,
        config: TaskRunConfig,
        progress_repo: TaskProgressRepository,
        sys_log_repo: PgSystemLogRepository,
    ) -> None:
        self.task_id = task_id
        self.started_at = started_at
        self.progress_repo = progress_repo
        self._config = config
        self._sys_log_repo = sys_log_repo
        self.completed_progress_data: dict[str, Any] = {}
        self.completed_details: dict[str, Any] = {}

    def report_running(self, *, progress_data: dict[str, Any]) -> None:
        """Upsert a RUNNING TaskProgress row. Used for both the initial
        (`processed: 0`) row and every periodic mid-task update.
        """
        self.progress_repo.upsert(
            TaskProgress(
                task_id=self.task_id,
                task_type=self._config.task_type,
                status=TaskStatus.RUNNING,
                progress_data=progress_data,
                started_at=self.started_at,
                updated_at=datetime.now(UTC),
            )
        )

    def log_started(self, *, details: dict[str, Any]) -> None:
        """Write the INFO "started" SystemLog. Call once, after the first
        `report_running`, once the caller's pre-count details are known.
        """
        self._sys_log_repo.create(
            SystemLog(
                category=self._config.log_category,
                level=LogLevel.INFO,
                message=self._config.messages.started,
                trace_id=self.task_id,
                details=details,
            )
        )

    def set_completed(self, *, progress_data: dict[str, Any], details: dict[str, Any]) -> None:
        """Stage the payloads `task_run` writes on normal (non-exception)
        exit: a COMPLETED TaskProgress upsert, then an INFO "completed"
        SystemLog. Call once, as the last thing the wrapped block does.
        """
        self.completed_progress_data = progress_data
        self.completed_details = details


@contextmanager
def task_run(database_url: str, config: TaskRunConfig) -> Iterator[TaskRunHandle]:
    """Own a task's RUNNING/COMPLETED/FAILED lifecycle envelope.

    Opens a dedicated autocommit connection for TaskProgress + SystemLog
    writes (separate from whatever transactional connection(s) the caller's
    business logic uses, so telemetry survives a business-transaction
    rollback and stays visible to the async `/ws` reader immediately). On
    normal exit, writes the handle's staged COMPLETED payload. On exception,
    writes a FAILED row (`processed`/`total` from `config.read_progress()`,
    plus `error`) and an ERROR SystemLog with a traceback — each write
    independently suppressed so a telemetry failure never masks the
    original exception — then re-raises.
    """
    task_id = uuid.uuid4().hex
    started_at = datetime.now(UTC)
    progress_conn = connect_sync(database_url, autocommit=True)
    try:
        progress_repo = PgTaskProgressRepository(progress_conn)
        sys_log_repo = PgSystemLogRepository(progress_conn)
        handle = TaskRunHandle(
            task_id=task_id,
            started_at=started_at,
            config=config,
            progress_repo=progress_repo,
            sys_log_repo=sys_log_repo,
        )

        try:
            yield handle
        except Exception as exc:
            processed, total = config.read_progress()
            with contextlib.suppress(Exception):
                progress_repo.upsert(
                    TaskProgress(
                        task_id=task_id,
                        task_type=config.task_type,
                        status=TaskStatus.FAILED,
                        progress_data={
                            "processed": processed,
                            "total": total,
                            "error": str(exc),
                        },
                        started_at=started_at,
                        updated_at=datetime.now(UTC),
                        completed_at=datetime.now(UTC),
                    )
                )
            with contextlib.suppress(Exception):
                sys_log_repo.create(
                    SystemLog(
                        category=config.log_category,
                        level=LogLevel.ERROR,
                        message=config.messages.failed,
                        trace_id=task_id,
                        details={"error": str(exc), "traceback": traceback.format_exc()},
                    )
                )
            raise
        else:
            progress_repo.upsert(
                TaskProgress(
                    task_id=task_id,
                    task_type=config.task_type,
                    status=TaskStatus.COMPLETED,
                    progress_data=handle.completed_progress_data,
                    started_at=started_at,
                    updated_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                )
            )
            sys_log_repo.create(
                SystemLog(
                    category=config.log_category,
                    level=LogLevel.INFO,
                    message=config.messages.completed,
                    trace_id=task_id,
                    details=handle.completed_details,
                )
            )
    finally:
        progress_conn.close()
