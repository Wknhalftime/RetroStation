"""Unit tests for run_hash_backfill: batching, progress and liveness."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.library import LibraryFile
from backend.domain.system import TaskProgress
from backend.tasks.library_hash_backfill_tasks import BackfillRunConfig, run_hash_backfill
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
