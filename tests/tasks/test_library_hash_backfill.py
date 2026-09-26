"""Unit tests for run_hash_backfill: batching, progress and liveness."""
from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import structlog
from structlog.testing import capture_logs

import backend.tasks.library_hash_backfill_tasks as library_hash_backfill_tasks_module
from backend.domain.enums import TaskStatus, TaskType
from backend.domain.library import LibraryFile
from backend.domain.system import TaskProgress
from backend.tasks.library_hash_backfill_tasks import (
    BackfillRunConfig,
    library_hash_backfill_resume,
    run_hash_backfill,
)
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.task_progress import FakeTaskProgressRepository

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _library(tmp_path: Path, n: int) -> tuple[FakeLibraryFileRepository, list[Path]]:
    """n files on disk, each indexed as an unhashed row with its current stat."""
    repo = FakeLibraryFileRepository()
    paths = []
    for i in range(n):
        path = tmp_path / f"{i:02d}.flac"
        path.write_bytes(bytes([i]) * (100 + i))
        st = path.stat()
        repo.upsert(LibraryFile(
            id=uuid4(), file_path=str(path), file_hash=None, format="flac",
            file_size=st.st_size, file_mtime_ns=st.st_mtime_ns,
        ))
        paths.append(path)
    return repo, paths


def _other_run(updated_at: datetime) -> TaskProgress:
    return TaskProgress(
        task_id="other-run", task_type=TaskType.HASH_BACKFILL, status=TaskStatus.RUNNING,
        progress_data={}, started_at=updated_at - timedelta(minutes=5), updated_at=updated_at,
    )


def test_hashes_every_row_across_batches_and_reports_completion(tmp_path: Path) -> None:
    files, _ = _library(tmp_path, 5)
    progress = FakeTaskProgressRepository()
    commits: list[int] = []

    result = run_hash_backfill(
        files, progress, lambda: commits.append(1),
        BackfillRunConfig(run_id="run-1", batch_size=2, clock=lambda: T0),
    )

    assert result is not None
    assert result.hashed == 5
    assert files.count_unhashed() == 0
    final = progress.get_by_id("run-1")
    assert final is not None
    assert final.status == TaskStatus.COMPLETED
    assert final.task_type == TaskType.HASH_BACKFILL
    assert (final.progress_data["processed"], final.progress_data["total"]) == (5, 5)
    assert [u.status for u in progress.received_upserts] == [
        TaskStatus.RUNNING, TaskStatus.RUNNING, TaskStatus.RUNNING, TaskStatus.COMPLETED,
    ]
    assert len(commits) == 4


def test_nothing_to_hash_writes_no_progress(tmp_path: Path) -> None:
    progress = FakeTaskProgressRepository()
    result = run_hash_backfill(
        FakeLibraryFileRepository(), progress, lambda: None,
        BackfillRunConfig(run_id="run-1", clock=lambda: T0),
    )
    assert result is None
    assert progress.received_upserts == []


def test_does_not_start_while_another_run_is_live(tmp_path: Path) -> None:
    files, _ = _library(tmp_path, 2)
    progress = FakeTaskProgressRepository()
    progress.upsert(_other_run(updated_at=T0 - timedelta(minutes=2)))

    result = run_hash_backfill(
        files, progress, lambda: None, BackfillRunConfig(run_id="run-2", clock=lambda: T0),
    )

    assert result is None
    assert files.count_unhashed() == 2


def test_takes_over_from_a_run_that_stopped_updating(tmp_path: Path) -> None:
    files, _ = _library(tmp_path, 2)
    progress = FakeTaskProgressRepository()
    progress.upsert(_other_run(updated_at=T0 - timedelta(minutes=30)))

    result = run_hash_backfill(
        files, progress, lambda: None, BackfillRunConfig(run_id="run-2", clock=lambda: T0),
    )

    assert result is not None
    assert result.hashed == 2


def test_ends_even_when_rows_cannot_be_hashed(tmp_path: Path) -> None:
    files, paths = _library(tmp_path, 3)
    paths[0].unlink()
    paths[1].write_bytes(b"edited")

    result = run_hash_backfill(
        files, FakeTaskProgressRepository(), lambda: None,
        BackfillRunConfig(run_id="run-1", batch_size=1, clock=lambda: T0),
    )

    assert result is not None
    assert (result.hashed, result.changed, result.unreadable) == (1, 1, 1)
    assert files.count_unhashed() == 2


def test_run_that_hashes_nothing_posts_no_progress(tmp_path: Path) -> None:
    files, paths = _library(tmp_path, 2)
    paths[0].unlink()
    paths[1].write_bytes(b"edited-to-a-different-size")
    progress = FakeTaskProgressRepository()

    result = run_hash_backfill(
        files, progress, lambda: None,
        BackfillRunConfig(run_id="run-1", clock=lambda: T0),
    )

    assert result is not None
    assert (result.hashed, result.changed, result.unreadable) == (0, 1, 1)
    assert progress.received_upserts == []


@pytest.fixture
def _debug_bound_logger() -> Generator[None]:
    """Make DEBUG-level events visible to `capture_logs` for this test.

    `capture_logs` swaps the processor chain but not `wrapper_class` (see
    `tests/services/test_mb_client_observability.py`). Importing this task
    module pulls in `backend.tasks.huey_app`, which calls `configure_logging`
    at import time with the app's default level (INFO) — so without this
    override, a `logger.debug(...)` call never reaches the processor chain
    and `capture_logs` would silently see nothing, whether or not the
    production code actually logs at DEBUG.
    """
    original_config = structlog.get_config()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        yield
    finally:
        structlog.configure(**original_config)


def test_run_that_hashes_nothing_logs_nothing_above_debug(
    tmp_path: Path, _debug_bound_logger: None,
) -> None:
    files, paths = _library(tmp_path, 2)
    paths[0].unlink()
    paths[1].write_bytes(b"edited-to-a-different-size")

    with capture_logs() as captured:
        result = run_hash_backfill(
            files, FakeTaskProgressRepository(), lambda: None,
            BackfillRunConfig(run_id="run-1", clock=lambda: T0),
        )

    assert result is not None
    assert result.hashed == 0
    assert not any(e["log_level"] in {"info", "warning", "error"} for e in captured)


def test_run_that_hashes_files_logs_completion_at_info(
    tmp_path: Path, _debug_bound_logger: None,
) -> None:
    files, _ = _library(tmp_path, 2)

    with capture_logs() as captured:
        result = run_hash_backfill(
            files, FakeTaskProgressRepository(), lambda: None,
            BackfillRunConfig(run_id="run-1", clock=lambda: T0),
        )

    assert result is not None
    assert result.hashed == 2
    complete_events = [e for e in captured if e["event"] == "hash_backfill_complete"]
    assert len(complete_events) == 1
    assert complete_events[0]["log_level"] == "info"


def test_resume_calls_the_backfill_task_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[None] = []

    class _StubTask:
        def call_local(self) -> None:
            calls.append(None)

    monkeypatch.setattr(
        library_hash_backfill_tasks_module, "library_hash_backfill_task", _StubTask()
    )

    library_hash_backfill_resume.call_local()

    assert len(calls) == 1
