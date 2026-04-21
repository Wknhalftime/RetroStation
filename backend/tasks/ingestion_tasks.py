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
    return {
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
            nonlocal last_processed
            last_processed = rows_processed
            # Best-effort telemetry: a progress-connection fault must never
            # fail an otherwise-successful ingestion. Matches FAILED branch.
            with contextlib.suppress(Exception):
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

        progress_repo.upsert(
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
            )
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
        error_code: str | None = None
        if isinstance(exc, DuplicatePlaylistError):
            error_code = "duplicate_playlist"

        if progress_repo is not None:
            # Suppress Exception (not BaseException) so a best-effort FAILED
            # write cannot mask the original ingestion exception via PEP 3134
            # __context__ replacement. KeyboardInterrupt / SystemExit /
            # GeneratorExit still propagate because they inherit from
            # BaseException. Matches library_scan_task precedent.
            with contextlib.suppress(Exception):
                progress_repo.upsert(
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
                    )
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
        if progress_conn is not None:
            progress_conn.close()


# Re-exported for backward compatibility with any module importing the
# exception types via this path. Silences "imported but unused" linting.
__all__ = [
    "CsvDecodeError",
    "DuplicatePlaylistError",
    "ingestion_task",
]
