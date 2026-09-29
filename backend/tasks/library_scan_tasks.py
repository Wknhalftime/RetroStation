from __future__ import annotations

import contextlib
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import structlog

from backend.config import get_settings
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import (
    EnrichmentStatus,
    LogCategory,
    LogLevel,
    PurgeMissingPolicy,
    TaskStatus,
    TaskType,
)
from backend.domain.library import (
    PURGE_MISSING_SETTING,
    LibraryFile,
    LibraryQuarantine,
    MissingFileChangedError,
    MissingFileDeletion,
    MissingFilePlan,
    MissingFileReconciliation,
)
from backend.domain.system import SystemLog, TaskProgress
from backend.repositories.library_files import LibraryFileRepository
from backend.services.folder_hash_service import diff_tree
from backend.services.grouping_service import assign_work
from backend.services.library_scan_service import (
    adopt_moved_row,
    clear_resolved_quarantine,
    mark_unseen_missing,
    quarantine_once,
    scan_directory,
)
from backend.services.missing_file_reconciliation_service import (
    ReconciliationRepos,
    apply_missing_file_move,
    plan_for_library,
    purge_unmatched_missing,
    repick_stranded_masters,
)
from backend.services.repository_factory import RepositoryFactory, reconciliation_repos
from backend.tasks._enqueue_chain import enqueue_or_log
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

COMMIT_CHUNK_SIZE = 100


def apply_missing_file_moves(
    plan: MissingFilePlan,
    conn: psycopg.Connection[Any],
    repos: ReconciliationRepos,
) -> tuple[int, int, int]:
    """Apply each move in a transaction of its own. Returns (folded, failed, stale).

    On a connection already in a transaction each move is a savepoint, so a
    move the database refuses rolls back alone and the rest still fold. A
    move the library has overtaken since planning is stale, not failed.
    """
    folded = failed = stale = 0
    for move in plan.moves:
        try:
            with conn.transaction():
                did_fold = apply_missing_file_move(move, repos)
        except psycopg.Error as exc:
            logger.warning(
                "missing_file_fold_failed",
                missing_path=move.missing_path,
                successor_path=move.successor_path,
                error=str(exc),
            )
            failed += 1
            continue
        if did_fold:
            folded += 1
        else:
            stale += 1
    return folded, failed, stale


def reconcile_missing_after_scan(
    library_conn: psycopg.Connection[Any],
    repos: RepositoryFactory,
) -> MissingFileReconciliation | None:
    """Fold missing rows into their successors, then re-pick stranded masters.

    Runs after the scan and grouping have committed, so a refusal here
    rolls back only the reconciliation. A fold the database refuses is
    skipped on its own; a failure outside one fold (planning, the master
    sweep, the commit) rolls back the whole run and returns None.
    """
    recon_repos = reconciliation_repos(repos)
    try:
        plan = plan_for_library(recon_repos.files)
        folded, failed, stale = apply_missing_file_moves(plan, library_conn, recon_repos)
        repicked = repick_stranded_masters(recon_repos)
        library_conn.commit()
    except psycopg.Error:
        library_conn.rollback()
        logger.warning("missing_reconciliation_failed", exc_info=True)
        return None
    result = MissingFileReconciliation(
        reconciled=folded,
        failed=failed,
        ambiguous=len(plan.ambiguous),
        unmatched=len(plan.unmatched),
        masters_repicked=repicked,
    )
    logger.info(
        "missing_files_reconciled",
        reconciled=result.reconciled,
        failed=result.failed,
        stale=stale,
        ambiguous=result.ambiguous,
        unmatched=result.unmatched,
        masters_repicked=result.masters_repicked,
    )
    return result


def purge_missing_after_scan(
    library_conn: psycopg.Connection[Any],
    repos: RepositoryFactory,
    root: Path,
    walk_saw_files: bool,
) -> MissingFileDeletion | None:
    """With library.purge_missing = after_scan, delete missing rows nothing replaces.

    Skipped when the walk saw no files (probably an unmounted drive), as
    mark_unseen_missing is. Its own transaction: a refusal, or a row a concurrent
    scan restored, rolls back only the purge.
    """
    setting = repos.user_settings.get(PURGE_MISSING_SETTING)
    policy = PurgeMissingPolicy.from_setting(setting.value if setting else None)
    if not walk_saw_files or policy != PurgeMissingPolicy.AFTER_SCAN:
        return None
    try:
        result = purge_unmatched_missing(
            str(root), reconciliation_repos(repos), repos.broadcast_identities
        )
        library_conn.commit()
    except (psycopg.Error, MissingFileChangedError):
        library_conn.rollback()
        logger.warning("missing_purge_failed", exc_info=True)
        return None
    logger.info(
        "missing_files_purged",
        deleted=result.deleted,
        matches_released=result.matches_released,
    )
    return result


def _row_to_group(
    lf: LibraryFile,
    indexed_before: set[str],
    file_repo: LibraryFileRepository,
) -> LibraryFile | None:
    """The stored row the grouping pass should give a work, or None if it needs none.

    A path first indexed by this scan was stored as read, id and all. Any
    other path kept its stored id (the upsert keys on the path, so *lf*'s
    fresh id exists nowhere) and its work, so its stored row is the one to
    group, and only while it has no work.
    """
    row = file_repo.get_by_path(lf.file_path) if lf.file_path in indexed_before else lf
    if row is None or row.work_id is not None:
        return None
    return row


def _run_scan(
    *,
    root_path: str,
    library_conn: psycopg.Connection,
    repos: RepositoryFactory,
    progress_repo: PgTaskProgressRepository,
    task_id: str,
    chunk_size: int = COMMIT_CHUNK_SIZE,
) -> tuple[int, int, dict[str, object]]:
    """Core scan logic — extracted from the Huey task so it is directly testable.

    Opens no connections itself; callers provide them.
    Returns ``(files_written, quarantine_written, last_progress)``.
    """
    task_started_at = datetime.now(UTC)
    last_progress: dict[str, object] = {
        "processed": 0,
        "total": 0,
        "current_path": "",
    }

    files_written = 0
    files_committed = 0
    quarantine_written = 0
    # New paths that adopted the row of a moved file, keeping its id.
    relocated_paths: set[str] = set()
    pending_writes = 0
    written_files: list[LibraryFile] = []
    # Read cleanly but could not be stored; quarantined like a parse failure.
    insert_failed: set[str] = set()

    # Stored paths use the OS separator; a root typed as ``D:/Music``
    # would otherwise match none of them.
    root = Path(root_path)
    # An empty library has nothing a new file could have moved from, so a
    # first scan skips move detection.
    library_was_empty = not repos.library_files.has_any()
    # Paths already indexed, so a path seen for the first time can be
    # checked for being a moved or renamed file before it is inserted.
    known_paths = repos.library_files.get_path_statuses_under(str(root))

    # --- Callbacks ---

    def _flush_chunk() -> None:
        nonlocal pending_writes, files_committed
        library_conn.commit()
        files_committed += pending_writes
        pending_writes = 0

    def on_file(lf: LibraryFile) -> None:
        nonlocal pending_writes, files_written
        try:
            if (
                not library_was_empty
                and lf.file_path not in known_paths
                and adopt_moved_row(lf, repos.library_files) is not None
            ):
                relocated_paths.add(lf.file_path)
            repos.library_files.upsert_write_only(lf)
        except psycopg.Error:
            # Bad metadata (e.g. null bytes) can poison the transaction.
            # Rollback discards uncommitted chunk rows — they will be
            # re-inserted on the next scan since the upsert is idempotent.
            library_conn.rollback()
            logger.warning(
                "file_insert_failed_quarantined",
                file_path=lf.file_path,
                exc_info=True,
            )
            quarantine_once(
                repos.library_quarantine,
                lf.file_path,
                "metadata contains invalid characters",
            )
            insert_failed.add(lf.file_path)
            pending_writes = 1  # quarantine row is only uncommitted write
            return
        files_written += 1
        pending_writes += 1
        written_files.append(lf)
        if pending_writes >= chunk_size:
            _flush_chunk()

    def on_quarantine(entry: LibraryQuarantine) -> None:
        nonlocal pending_writes, quarantine_written
        quarantine_once(repos.library_quarantine, entry.file_path, entry.error_message)
        quarantine_written += 1
        pending_writes += 1
        if pending_writes >= chunk_size:
            _flush_chunk()

    def on_progress(processed: int, total: int, current_path: str) -> None:
        nonlocal last_progress
        last_progress = {
            "processed": processed,
            "total": total,
            "current_path": current_path,
            "files_committed": files_committed,
        }
        progress_repo.upsert(
            TaskProgress(
                task_id=task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.RUNNING,
                progress_data=last_progress,
                started_at=task_started_at,
                updated_at=datetime.now(UTC),
            )
        )

    # --- Run scan with callbacks ---
    scanned, quarantined = scan_directory(
        root,
        on_progress=on_progress,
        on_file=on_file,
        on_quarantine=on_quarantine,
    )

    # Flush any remaining scan writes before folder-tree pass
    if pending_writes > 0:
        _flush_chunk()

    # Quarantined files are still on disk, so they count as seen.
    seen_paths = {lf.file_path for lf in scanned} | {q.file_path for q in quarantined}
    files_missing = mark_unseen_missing(root, seen_paths, repos.library_files)
    # Same rule as marking missing: a walk that saw nothing judges nothing.
    quarantine_cleared = 0
    if seen_paths:
        quarantine_cleared = clear_resolved_quarantine(
            repos.library_quarantine.get_paths_under(str(root)),
            {q.file_path for q in quarantined} | insert_failed,
            repos.library_quarantine,
        )
    logger.info(
        "scan_reconciled",
        relocated=len(relocated_paths),
        marked_missing=files_missing,
        quarantine_cleared=quarantine_cleared,
    )

    # Build folder tree so library_folders is populated even on first scan
    diff_tree(root_path, repos.library_folders)

    # Commit unconditionally: diff_tree always writes folder rows to library_conn
    # regardless of whether any audio files were scanned, so we must not guard
    # this commit behind pending_writes > 0.
    library_conn.commit()

    # --- Grouping pass: assign work_id to written rows that have none ---
    grouped = 0
    grouping_pending = 0
    indexed_before = set(known_paths) | relocated_paths
    for lf in written_files:
        row = _row_to_group(lf, indexed_before, repos.library_files)
        if row is None:
            continue
        try:
            result = assign_work(
                row,
                artist_repo=repos.artists,
                work_repo=repos.works,
                library_file_repo=repos.library_files,
                recording_repo=repos.recordings,
                song_master_repo=repos.song_masters,
            )
            if result:
                repos.library_files.update_work_id(row.id, result.work_id)
                if result.recording_id:
                    repos.library_files.update_recording_link(
                        row.id,
                        result.recording_id,
                        EnrichmentStatus.PENDING,
                    )
                grouped += 1
                grouping_pending += 1
                if grouping_pending >= chunk_size:
                    library_conn.commit()
                    grouping_pending = 0
        # Narrow to failure modes we expect per-file: DB errors for any
        # repo write, ValueError from malformed metadata, OSError if a
        # downstream tagger touches the file. Logic bugs (KeyError,
        # AttributeError, TypeError) propagate to the task boundary so
        # they surface instead of silently logging and continuing.
        except (psycopg.Error, ValueError, OSError) as exc:
            logger.warning(
                "grouping_failed",
                file_id=str(row.id),
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            if isinstance(exc, psycopg.Error):
                library_conn.rollback()
                grouping_pending = 0
    if grouping_pending > 0:
        library_conn.commit()
    if grouped > 0:
        logger.info("scan_grouping_complete", grouped=grouped, total=len(written_files))

    # Successors need their work (grouping above) before rows fold into them.
    if reconcile_missing_after_scan(library_conn, repos) is not None:
        # Only after a reconciliation that ran: what it could not fold may be purged.
        purge_missing_after_scan(library_conn, repos, root, walk_saw_files=bool(seen_paths))

    return files_written, quarantine_written, last_progress


@huey.task()  # type: ignore[untyped-decorator]
def library_scan_task(root_path: str) -> str:
    """Scan a directory for audio files and persist results to the DB."""
    logger.info("library_scan_task_started", root=root_path)
    settings = get_settings()
    task_id = uuid.uuid4().hex
    task_started_at = datetime.now(UTC)
    last_progress: dict[str, object] = {
        "processed": 0,
        "total": 0,
        "current_path": "",
    }

    progress_conn = None
    progress_repo: PgTaskProgressRepository | None = None
    sys_log_repo: PgSystemLogRepository | None = None
    library_conn = None

    try:
        # Autocommit connection for progress tracking and system logs
        progress_conn = connect_sync(
            settings.database_url,
            autocommit=True,
        )
        progress_repo = PgTaskProgressRepository(progress_conn)
        sys_log_repo = PgSystemLogRepository(progress_conn)

        # Initial progress record
        progress_repo.upsert(
            TaskProgress(
                task_id=task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.RUNNING,
                progress_data=last_progress,
                started_at=task_started_at,
                updated_at=task_started_at,
            )
        )

        sys_log_repo.create(
            SystemLog(
                category=LogCategory.SCAN,
                level=LogLevel.INFO,
                message="scan_started",
                trace_id=task_id,
                details={"root": root_path},
            )
        )

        # Open library connection BEFORE scan so callbacks can write immediately
        library_conn = connect_sync(
            settings.database_url,
            autocommit=False,
        )
        repos = RepositoryFactory(library_conn)

        files_written, quarantine_written, last_progress = _run_scan(
            root_path=root_path,
            library_conn=library_conn,
            repos=repos,
            progress_repo=progress_repo,
            task_id=task_id,
        )

        if files_written == 0:
            last_progress["warning"] = "no_audio_files_found"
            logger.warning("library_scan_zero_files", root=root_path)

        # Mark completed AFTER library data commit succeeds
        progress_repo.upsert(
            TaskProgress(
                task_id=task_id,
                task_type=TaskType.SCAN,
                status=TaskStatus.COMPLETED,
                progress_data=last_progress,
                started_at=task_started_at,
                updated_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )

        sys_log_repo.create(
            SystemLog(
                category=LogCategory.SCAN,
                level=LogLevel.INFO,
                message="scan_completed",
                trace_id=task_id,
                details={"files_indexed": files_written, "quarantined": quarantine_written},
            )
        )

        logger.info(
            "library_scan_task_complete",
            root=root_path,
            files_indexed=files_written,
            quarantined=quarantine_written,
        )

        # Fire-and-forget: chain into enrichment if any files were written.
        # Each enqueue is guarded independently (AUD-R011 decision 1): this
        # scan's own run already reported COMPLETED above, so a downstream
        # enqueue failure must not retroactively flip it to FAILED. The
        # caller owns the handoff — an enqueue failure is logged on this
        # scan's own task_id and the second enqueue is still attempted even
        # if the first one failed.
        if files_written > 0:
            from backend.tasks.library_enrichment_tasks import library_enrichment_task
            from backend.tasks.library_hash_backfill_tasks import library_hash_backfill_task

            # Fingerprinting first: it is disk-bound but mostly header reads,
            # short next to enrichment's MusicBrainz lookups. Until it finishes,
            # move detection falls back to size + mtime for MP3s and FLACs
            # without a stored MD5. A no-op when nothing is waiting.
            enqueue_or_log(
                library_hash_backfill_task,
                task_name="library_hash_backfill_task",
                caller_task_id=task_id,
                log_category=LogCategory.SCAN,
                sys_log_repo=sys_log_repo,
            )
            enqueue_or_log(
                library_enrichment_task,
                task_name="library_enrichment_task",
                caller_task_id=task_id,
                log_category=LogCategory.SCAN,
                sys_log_repo=sys_log_repo,
            )

    except Exception as exc:
        if library_conn is not None:
            with contextlib.suppress(Exception):
                library_conn.rollback()

        if progress_conn is not None and progress_repo is not None:
            with contextlib.suppress(Exception):
                progress_repo.upsert(
                    TaskProgress(
                        task_id=task_id,
                        task_type=TaskType.SCAN,
                        status=TaskStatus.FAILED,
                        progress_data={**last_progress, "error": str(exc)},
                        started_at=task_started_at,
                        updated_at=datetime.now(UTC),
                        completed_at=datetime.now(UTC),
                    )
                )
        if progress_conn is not None and sys_log_repo is not None:
            with contextlib.suppress(Exception):
                sys_log_repo.create(
                    SystemLog(
                        category=LogCategory.SCAN,
                        level=LogLevel.ERROR,
                        message="scan_failed",
                        trace_id=task_id,
                        details={"error": str(exc), "traceback": traceback.format_exc()},
                    )
                )
        raise

    finally:
        if library_conn is not None:
            library_conn.close()
        if progress_conn is not None:
            progress_conn.close()

    return root_path
