"""How a cue run's progress row ends (PR G2 final review M7 and I1; not a locked test).

Requirements:
- M7: a FAILED cue row says why, in ``progress_data["error"]`` (the bottom bar shows it after
  the "Failed" label, as it does for scans);
- I1: a cue run's row that a crash or a hard stop of the cue worker left RUNNING is ended at
  the worker's next start: FAILED, with a reason, and ``completed_at = updated_at`` (outside
  the ``/ws`` feed's 5 s grace, so it never flashes in the bottom bar); no other row is
  touched (the meter's, a scan's, an ended cue row).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.streaming import CueCoverage
from backend.domain.system import TaskProgress
from backend.services.streaming.cue_progress import (
    FAILED_REASON,
    LEFTOVER_REASON,
    CueProgressPorts,
    CueProgressRows,
    end_leftover_cue_rows_with,
)
from tests.fakes.task_progress import FakeTaskProgressRepository

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
HOUR_AGO = T0 - timedelta(hours=1)


def rows_for(written: list[TaskProgress]) -> CueProgressRows:
    return CueProgressRows(
        CueProgressPorts(
            write=written.append,
            coverage=lambda: CueCoverage(analysable=10, ready=2, failed=1, unhashed=0),
            clock=lambda: T0,
            run_id="run-1",
        )
    )


def test_a_failed_run_says_why() -> None:
    written: list[TaskProgress] = []
    rows = rows_for(written)
    rows.batch_stored(2)
    rows.run_ended(failed=True)
    failed = written[-1]
    assert failed.status == TaskStatus.FAILED
    assert failed.progress_data == {"processed": 3, "total": 10, "error": FAILED_REASON}
    assert 0 < len(FAILED_REASON) <= 80


def test_a_completed_run_carries_no_error() -> None:
    written: list[TaskProgress] = []
    rows = rows_for(written)
    rows.batch_stored(2)
    rows.run_ended(failed=False)
    assert written[-1].progress_data == {"processed": 3, "total": 10}


def row(task_id: str, task_type: TaskType, status: TaskStatus) -> TaskProgress:
    ended = None if status == TaskStatus.RUNNING else HOUR_AGO
    return TaskProgress(
        task_id, task_type, status, {"processed": 4, "total": 9}, HOUR_AGO, HOUR_AGO, ended
    )


def test_a_cue_row_left_running_is_failed_without_flashing() -> None:
    repo = FakeTaskProgressRepository()
    left = row("run-old", TaskType.CUE_ANALYSIS, TaskStatus.RUNNING)
    others = [
        row("run-done", TaskType.CUE_ANALYSIS, TaskStatus.COMPLETED),
        row("scan-1", TaskType.SCAN, TaskStatus.RUNNING),
        row("stream_resources", TaskType.STREAM_RESOURCES, TaskStatus.RUNNING),
    ]
    for task in (left, *others):
        repo.upsert(task)
    before = len(repo.received_upserts)

    end_leftover_cue_rows_with(repo)

    [ended] = repo.received_upserts[before:]
    assert ended.task_id == "run-old"
    assert ended.status == TaskStatus.FAILED
    assert ended.completed_at == ended.updated_at == HOUR_AGO
    assert ended.started_at == HOUR_AGO
    assert ended.progress_data == {"processed": 4, "total": 9, "error": LEFTOVER_REASON}
    for task in others:
        assert repo.get_by_id(task.task_id) == task


def test_with_no_cue_row_left_running_nothing_is_written() -> None:
    repo = FakeTaskProgressRepository()
    repo.upsert(row("scan-1", TaskType.SCAN, TaskStatus.RUNNING))
    before = len(repo.received_upserts)
    end_leftover_cue_rows_with(repo)
    assert repo.received_upserts[before:] == []
