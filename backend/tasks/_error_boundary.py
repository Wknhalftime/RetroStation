"""Shared error-boundary helper for Huey background tasks.

Pipeline tasks (artist_matching, identity_matching, embedding) that don't
write per-item TaskProgress during normal operation still need to report
failures so the UI surfaces them. This module provides a single context
manager that:

1. Catches any exception at the task boundary.
2. Writes a FAILED TaskProgress row via a dedicated autocommit connection
   (independent of the task's main transactional connection, which may be
   rolled back).
3. Writes a SystemLog ERROR entry with the task_id trace.
4. Re-raises so Huey's worker-level failure handling still runs.

Reference pattern: `backend.tasks.mb_enrichment_tasks.mb_enrichment_task`.
The helper extracts the outer try/except into one place so every pipeline
task shares the same reporting shape.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import structlog

from backend.config import get_settings
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import LogCategory, LogLevel, TaskStatus, TaskType
from backend.domain.system import SystemLog, TaskProgress

logger = structlog.get_logger()


@contextmanager
def task_failure_telemetry(
    task_type: TaskType,
    log_category: LogCategory,
    *,
    task_id: str | None = None,
) -> Iterator[str]:
    """Write a FAILED TaskProgress + ERROR SystemLog when the wrapped task raises.

    Yields the task_id so callers can correlate their own logs. Use when a
    Huey task should report terminal failures to the UI but does not already
    maintain per-item TaskProgress telemetry (which is the case for the
    matching and embedding pipeline tasks).

    The telemetry connection is separate from the task's main connection and
    uses autocommit, so failure rows still land even when the main connection
    was rolled back by the failing transaction.

    Args:
        task_type: `TaskType` enum for the TaskProgress row.
        log_category: `LogCategory` for the SystemLog entry.
        task_id: Optional pre-generated trace id. When omitted, a hex UUID4
            is generated so callers that log before entering the boundary
            can still correlate.

    Yields:
        The task_id (generated or passed through).

    Raises:
        Re-raises the original exception after telemetry is written. If
        telemetry writes themselves fail, they are suppressed so the
        primary exception is not masked.
    """
    settings = get_settings()
    resolved_task_id = task_id or uuid.uuid4().hex
    started_at = datetime.now(UTC)

    try:
        yield resolved_task_id
    except Exception as exc:
        _report_failure(
            database_url=settings.database_url,
            task_id=resolved_task_id,
            task_type=task_type,
            log_category=log_category,
            started_at=started_at,
            error_message=str(exc),
        )
        raise


def _report_failure(
    *,
    database_url: str,
    task_id: str,
    task_type: TaskType,
    log_category: LogCategory,
    started_at: datetime,
    error_message: str,
) -> None:
    """Write FAILED TaskProgress + ERROR SystemLog. Swallow telemetry errors
    so the primary exception stays the dominant signal in logs.
    """
    now = datetime.now(UTC)
    try:
        with connect_sync(database_url, autocommit=True) as telemetry_conn:
            progress_repo = PgTaskProgressRepository(telemetry_conn)
            sys_log_repo = PgSystemLogRepository(telemetry_conn)

            # Each write is guarded independently so a progress-row failure
            # doesn't block the system-log entry (and vice versa). Failures
            # are logged so the UI's "stuck in RUNNING" state is diagnosable
            # rather than silent.
            try:
                progress_repo.upsert(TaskProgress(
                    task_id=task_id,
                    task_type=task_type,
                    status=TaskStatus.FAILED,
                    progress_data={"error": error_message},
                    started_at=started_at,
                    updated_at=now,
                    completed_at=now,
                ))
            except Exception as progress_exc:  # noqa: BLE001
                logger.warning(
                    "task_failure_progress_write_failed",
                    task_id=task_id,
                    task_type=task_type.value,
                    primary_error=error_message,
                    telemetry_error=str(progress_exc),
                )

            try:
                sys_log_repo.create(SystemLog(
                    category=log_category,
                    level=LogLevel.ERROR,
                    message=f"{task_type.value}_failed",
                    trace_id=task_id,
                    details={"error": error_message},
                ))
            except Exception as syslog_exc:  # noqa: BLE001
                logger.warning(
                    "task_failure_systemlog_write_failed",
                    task_id=task_id,
                    task_type=task_type.value,
                    primary_error=error_message,
                    telemetry_error=str(syslog_exc),
                )
    except Exception as telemetry_exc:  # noqa: BLE001
        # Outer boundary: the telemetry connection itself failed. Log via
        # structlog and continue to re-raise the primary exception so the
        # real failure isn't masked.
        logger.warning(
            "task_failure_telemetry_connect_failed",
            task_id=task_id,
            task_type=task_type.value,
            primary_error=error_message,
            telemetry_error=str(telemetry_exc),
        )
