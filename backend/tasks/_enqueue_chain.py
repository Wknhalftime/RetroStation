"""Guarded enqueue for Huey task-chaining handoffs.

Several tasks in the pipeline chain into the next stage by calling another
`@huey.task()`-decorated function, e.g. `mb_enrichment_task()` at the end of
`library_enrichment_task`. That call does not run the next task inline — it
only enqueues it (`TaskWrapper.__call__` -> `Huey.enqueue` -> the storage
backend's `enqueue`). A chained task's own RUN failure is already its own to
report (its own FAILED row and failed log, written by whichever envelope it
uses). But the ENQUEUE call itself can also fail, and per AUD-R012 (1) the
caller owns that handoff: it must report the enqueue failure on its own
trace_id and keep its own COMPLETED/FAILED status exactly as if the handoff
had never been attempted.

Every task-to-task hand-off goes through `enqueue_or_log` (AUD-R014).

`enqueue_or_log` wraps a zero-arg enqueue call, catching only the exception
types SqliteHuey's enqueue path actually raises, and reporting the failure
as an ERROR SystemLog instead of letting it propagate into the caller's own
success/failure accounting.
"""

from __future__ import annotations

import sqlite3
import traceback
from collections.abc import Callable

import structlog

from backend.domain.enums import LogCategory, LogLevel
from backend.domain.system import SystemLog
from backend.repositories.system_logs import SystemLogRepository

logger = structlog.get_logger()

# SqliteHuey's enqueue path (Huey.enqueue -> SqliteStorage.enqueue ->
# BaseSqlStorage.db()) re-raises the sqlite3 driver's own exception
# unwrapped: huey defines no enqueue-specific exception type, and
# `BaseSqlStorage.db()` only rolls back and re-raises (huey/storage.py:
# `except Exception: if commit: conn.rollback(); raise`). `sqlite3.Error` is
# the base class for every concrete failure the driver can raise at that
# call site (database is locked, disk I/O error, disk full, corrupted
# schema): OperationalError, DatabaseError, ProgrammingError,
# IntegrityError, InterfaceError, DataError, InternalError,
# NotSupportedError. `sqlite3.Warning` is NOT a subclass of `sqlite3.Error`
# and is not raised by execute()/commit() failures, so it is deliberately
# excluded — catching it would not narrow anything real and would invite a
# bare-except-shaped tuple.
_ENQUEUE_ERRORS: tuple[type[BaseException], ...] = (sqlite3.Error,)


def enqueue_or_log(
    enqueue: Callable[[], object],
    *,
    task_name: str,
    caller_task_id: str,
    log_category: LogCategory,
    sys_log_repo: SystemLogRepository,
    on_failure: Callable[[], None] | None = None,
) -> None:
    """Call `enqueue` (a zero-arg closure wrapping a Huey task call).

    On success, does nothing further. On a Huey/SQLite enqueue failure,
    writes an ERROR SystemLog on the caller's own `trace_id` instead of
    letting the failure escape: the caller owns the handoff, so an enqueue
    failure is the caller's problem to report, not a reason to change the
    caller's own COMPLETED/FAILED status. Any other exception type
    (a logic bug, not a storage fault) is not caught here and propagates
    to the caller's own error boundary.

    Args:
        enqueue: Zero-arg closure that performs the enqueue call, e.g.
            `lambda: mb_enrichment_task()`.
        task_name: The next task's name, used to build the SystemLog
            message `f"{task_name}_enqueue_failed"`.
        caller_task_id: The caller's own task_id, used as the SystemLog's
            `trace_id` so the failure is attributed to the run that
            attempted the handoff.
        log_category: `LogCategory` for the SystemLog entry.
        sys_log_repo: Repository to write the failure SystemLog to. Callers
            pass whichever connection/repo is open at the point of the
            enqueue call.
        on_failure: Optional zero-arg callback run after a storage failure has
            been reported (even when the SystemLog write itself failed), to undo
            what the caller prepared for the next task, e.g. the watcher poll
            releases the folders it staged. Not run on success or on any other
            exception. An exception it raises propagates: it is the caller's own
            work, not telemetry.
    """
    try:
        enqueue()
    except _ENQUEUE_ERRORS as exc:
        # This write must never raise: a telemetry fault here must not
        # surface in place of, or on top of, the enqueue failure it is
        # trying to report. Mirrors the guarded-write style already used in
        # `_error_boundary._report_failure` and `_task_run.task_run`.
        try:
            sys_log_repo.create(
                SystemLog(
                    category=log_category,
                    level=LogLevel.ERROR,
                    message=f"{task_name}_enqueue_failed",
                    trace_id=caller_task_id,
                    details={"error": str(exc), "traceback": traceback.format_exc()},
                )
            )
        except Exception as log_exc:  # noqa: BLE001 -- see comment above
            logger.warning(
                "enqueue_failure_systemlog_write_failed",
                caller_task_id=caller_task_id,
                next_task=task_name,
                enqueue_error=str(exc),
                telemetry_error=str(log_exc),
            )
        if on_failure is not None:
            on_failure()
