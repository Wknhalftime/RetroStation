from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
import structlog

from backend.config import get_settings
from backend.db.ingest_transaction import ingest_transaction
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.retry import retry_on_deadlock
from backend.db.sync_conn import connect_sync
from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.services.ingestion_service import (
    CsvDecodeError,
    CsvSchemaError,
    DuplicatePlaylistError,
    IngestionResult,
    count_csv_rows,
    ingest_csv,
)
from backend.tasks.huey_app import huey

logger = structlog.get_logger()


def _build_running_progress(
    processed: int, total: int, filename: str
) -> dict[str, Any]:
    return {"processed": processed, "total": total, "filename": filename}


def _build_completed_progress(
    result: IngestionResult, total: int, filename: str
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "processed": result.rows_processed,
        "skipped": result.rows_skipped,
        "total": total,
        "filename": filename,
        "playlist_id": result.playlist_id,
        "artists_created": result.artists_created,
        "identities_created": result.identities_created,
        "events_created": result.events_created,
        "broadcast_days_created": result.broadcast_days_created,
    }
    # A run that dropped rows still committed the rest, so it stays
    # COMPLETED — but it must not look clean. `warning` is the existing
    # progress_data convention (see library_scan_tasks) the frontend
    # renders in amber alongside a successful task.
    if result.rows_skipped > 0:
        payload["warning"] = "rows_skipped"
        payload["skip_reasons"] = dict(result.skip_reasons)
    return payload


# Exception type → stable code the frontend can branch on without parsing
# the human-readable message. Unmapped types fall through to `None`, which
# keeps the raw message as the only detail.
_ERROR_CODES: tuple[tuple[type[Exception], str], ...] = (
    (DuplicatePlaylistError, "duplicate_playlist"),
    (CsvSchemaError, "csv_schema_mismatch"),
    (CsvDecodeError, "csv_decode_failed"),
)


def _classify_error_code(exc: Exception) -> str | None:
    """Return the stable error code for ``exc``, or ``None`` if unmapped."""
    for exc_type, code in _ERROR_CODES:
        if isinstance(exc, exc_type):
            return code
    return None


def _build_failed_progress(
    exc: Exception,
    last_processed: int,
    total: int,
    filename: str,
    error_code: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": str(exc),
        "processed": last_processed,
        "total": total,
        "filename": filename,
    }
    if error_code is not None:
        payload["error_code"] = error_code
    return payload


# Transient-failure classes for progress-tracking writes. `OperationalError`
# is the psycopg base for every connectivity/timeout subclass we care about
# here: LockNotAvailable (lock_timeout on progress_tracking),
# QueryCanceled (statement_timeout), DeadlockDetected, ConnectionFailure,
# AdminShutdown / CrashShutdown, CannotConnectNow, DatabaseDropped, etc.
# `InterfaceError` covers cursor-closed / driver-state faults. `OSError`
# covers TCP/socket faults below the driver. Deliberately excludes
# IntegrityError, ProgrammingError, DataError, InternalError, and
# NotSupportedError so schema/constraint/syntax bugs surface loudly instead
# of being logged and swallowed as "telemetry dropped".
_PROGRESS_DROP_ERRORS: tuple[type[BaseException], ...] = (
    psycopg.OperationalError,
    psycopg.InterfaceError,
    OSError,
)


def _safe_progress_upsert(
    repo: PgTaskProgressRepository,
    task_progress: TaskProgress,
    *,
    task_id: str,
    lifecycle: str,
    **extra: Any,
) -> bool:
    """Best-effort progress upsert. Returns True on success, False on drop.

    Keeps the "telemetry must never change ingest outcome" invariant in one
    place. Callers that need throttling (e.g. mid-run) should implement
    their own try/except against ``_PROGRESS_DROP_ERRORS`` instead.
    """
    try:
        repo.upsert(task_progress)
        return True
    except _PROGRESS_DROP_ERRORS as telemetry_exc:
        logger.warning(
            "ingestion_progress_upsert_dropped",
            task_id=task_id,
            lifecycle=lifecycle,
            error=str(telemetry_exc),
            error_type=type(telemetry_exc).__name__,
            exc_info=True,
            **extra,
        )
        return False


@retry_on_deadlock(max_attempts=3, backoff_seconds=0.5)
def _run_ingest(
    file_bytes: bytes,
    file_name: str,
    station_id: str,
    *,
    on_row_processed: Callable[[int], None] | None = None,
    on_attempt_start: Callable[[], None] | None = None,
) -> IngestionResult:
    """Run a single ingestion transaction, retrying on Postgres deadlocks.

    ``on_attempt_start`` is invoked before any transactional work on every
    attempt — including retries triggered by advisory-lock or transaction
    deadlocks that fire before ``ingest_csv`` is entered. Callers use this
    to reset per-attempt bookkeeping such as ``last_processed``.

    The per-station advisory lock serialises same-station uploads; the
    retry decorator covers any cross-table contention the lock doesn't.
    """
    if on_attempt_start is not None:
        on_attempt_start()
    with ingest_transaction(station_id) as repos:
        return ingest_csv(
            file_bytes=file_bytes,
            file_name=file_name,
            station_id=station_id,
            playlist_repo=repos.broadcast_playlists,
            broadcast_artist_repo=repos.broadcast_artists,
            track_identity_repo=repos.broadcast_identities,
            play_event_repo=repos.broadcast_events,
            broadcast_day_repo=repos.broadcast_days,
            on_row_processed=on_row_processed,
        )


@huey.task()  # type: ignore[untyped-decorator]
def ingestion_task(
    file_bytes: bytes,
    file_name: str,
    station_id: str,
    task_id: str | None = None,
) -> str:
    """Ingest a CSV file with server-side progress tracking.

    ``task_id`` is normally minted by the router so the upload response
    and the ``progress_tracking`` row share one identity. It defaults
    to a fresh UUID only to keep this signature backward-compatible
    with the pre-progress-tracking 3-arg calls that SqliteHuey may have
    persisted across a deploy — without the default, those queued jobs
    would fail at execution with ``TypeError: missing 1 required
    positional argument``. The router always passes an explicit value.
    """
    if task_id is None:
        task_id = uuid.uuid4().hex
    settings = get_settings()
    task_started_at = datetime.now(UTC)
    last_processed = 0
    total_rows = 0
    # on_row_processed fires every _PROGRESS_REPORT_INTERVAL=100 rows. A
    # sustained progress-DB outage on a 100k-row ingest would emit ~1000
    # warnings-with-tracebacks. Log the first drop fully, then count the
    # rest; emit one summary on task exit.
    mid_run_drops = 0

    progress_conn: psycopg.Connection | None = None
    progress_repo: PgTaskProgressRepository | None = None

    try:
        progress_conn = connect_sync(settings.database_url, autocommit=True)
        progress_repo = PgTaskProgressRepository(progress_conn)
        # Non-optional alias so the row-callback closure doesn't need assert-narrowing.
        repo = progress_repo

        total_rows = count_csv_rows(file_bytes)

        progress_repo.upsert(
            TaskProgress(
                task_id=task_id,
                task_type=TaskType.INGESTION,
                status=TaskStatus.RUNNING,
                progress_data=_build_running_progress(0, total_rows, file_name),
                started_at=task_started_at,
                updated_at=task_started_at,
            )
        )

        def on_row_processed(rows_processed: int) -> None:
            nonlocal last_processed, mid_run_drops
            last_processed = rows_processed
            # Mid-run has its own try/except (not _safe_progress_upsert) to
            # throttle drop warnings: log only the first fully, count the
            # rest, emit one summary in `finally`.
            try:
                repo.upsert(
                    TaskProgress(
                        task_id=task_id,
                        task_type=TaskType.INGESTION,
                        status=TaskStatus.RUNNING,
                        progress_data=_build_running_progress(
                            rows_processed, total_rows, file_name
                        ),
                        started_at=task_started_at,
                        updated_at=datetime.now(UTC),
                    )
                )
            except _PROGRESS_DROP_ERRORS as telemetry_exc:
                if mid_run_drops == 0:
                    logger.warning(
                        "ingestion_progress_upsert_dropped",
                        task_id=task_id,
                        lifecycle="running",
                        processed=rows_processed,
                        error=str(telemetry_exc),
                        error_type=type(telemetry_exc).__name__,
                        exc_info=True,
                    )
                mid_run_drops += 1

        def on_attempt_start() -> None:
            nonlocal last_processed
            last_processed = 0

        result = _run_ingest(
            file_bytes,
            file_name,
            station_id,
            on_row_processed=on_row_processed,
            on_attempt_start=on_attempt_start,
        )

        # Ingest transaction has already committed inside _run_ingest, so a
        # fault writing COMPLETED must not flip the task to FAILED — that
        # would make durable data appear lost to the operator.
        _safe_progress_upsert(
            progress_repo,
            TaskProgress(
                task_id=task_id,
                task_type=TaskType.INGESTION,
                status=TaskStatus.COMPLETED,
                progress_data=_build_completed_progress(
                    result, total_rows, file_name
                ),
                started_at=task_started_at,
                updated_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            ),
            task_id=task_id,
            lifecycle="completed",
            playlist_id=result.playlist_id,
        )

        logger.info(
            "ingestion_task_complete",
            task_id=task_id,
            playlist_id=result.playlist_id,
        )

        # Embedding dispatch is decoupled from ingestion COMPLETED: the DB
        # commit has already happened, so a broker hiccup here is a separate
        # operational concern, not an ingestion failure.
        from backend.tasks.embedding_tasks import embedding_task

        with contextlib.suppress(Exception):
            embedding_task(result.playlist_id)

        return task_id

    except Exception as exc:
        error_code = _classify_error_code(exc)

        if progress_repo is not None:
            # Shadow guard: we're already in the top-level error handler
            # and `exc` is what must propagate. _safe_progress_upsert only
            # swallows transient driver errors, so a non-transient bug
            # (IntegrityError, ProgrammingError, etc.) would otherwise
            # replace `exc` before the `raise` below and hide the real
            # ingestion failure from the operator. Catch broadly here —
            # the project rule allows bare Exception at the top-level
            # error boundary, and that's exactly what this is.
            try:
                _safe_progress_upsert(
                    progress_repo,
                    TaskProgress(
                        task_id=task_id,
                        task_type=TaskType.INGESTION,
                        status=TaskStatus.FAILED,
                        progress_data=_build_failed_progress(
                            exc,
                            last_processed,
                            total_rows,
                            file_name,
                            error_code=error_code,
                        ),
                        started_at=task_started_at,
                        updated_at=datetime.now(UTC),
                        completed_at=datetime.now(UTC),
                    ),
                    task_id=task_id,
                    lifecycle="failed",
                    original_error=str(exc),
                )
            except Exception as shadow_exc:  # noqa: BLE001
                logger.warning(
                    "ingestion_progress_upsert_shadow_prevented",
                    task_id=task_id,
                    original_error=str(exc),
                    shadow_error=str(shadow_exc),
                    shadow_error_type=type(shadow_exc).__name__,
                    exc_info=True,
                )
        logger.warning(
            "ingestion_task_failed",
            task_id=task_id,
            error=str(exc),
            error_type=type(exc).__name__,
            error_code=error_code,
        )
        raise

    finally:
        if mid_run_drops > 1:
            logger.warning(
                "ingestion_progress_upsert_dropped_summary",
                task_id=task_id,
                dropped=mid_run_drops,
            )
        if progress_conn is not None:
            progress_conn.close()


# Re-exported for backward compatibility with any module importing the
# exception types via this path. Silences "imported but unused" linting.
__all__ = [
    "CsvDecodeError",
    "CsvSchemaError",
    "DuplicatePlaylistError",
    "ingestion_task",
]
