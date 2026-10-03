"""The cue run's progress row (D77a, D89; design note 9; I2, I7, M12).

D89 shows "cues ready X of Y" in the bottom bar, like scans and enrichment, through the
existing ``progress_tracking`` / ``/ws`` / ``ProgressBar`` machinery (D77a). The run tells its
progress sink two things: how much audio each batch stored, and how the run ended. The sink
is an observer: it never changes what the run stores or logs (I2).

- ``SilentProgress`` is the default sink: no row (a reported song, D79, and every run with no
  telemetry wired).
- ``CueProgressRows`` writes one row per storing run (M12), with its own id:
  - after the first batch that stores something, it reads the coverage once (design note 9:
    the count may be slow when cold), so the count includes that committed batch;
  - the row is RUNNING with ``processed`` (settled audio, ready or failed, D20) of ``total``
    (all analysable audio, PG7), and each later storing batch adds what it stored;
  - when the run ends the row is COMPLETED, or FAILED when the run ended in an error, with a
    short reason in ``progress_data["error"]`` (M7; the bottom bar shows it, as for a scan);
  - a coverage read that gives None (failed or timed out, I7) means this run writes no row;
  - an idle run, or one that stores nothing, reads nothing and writes nothing (D59, I2).
- ``end_leftover_cue_rows_with`` (I1): at the cue worker's start, a run's row that a crash or
  a hard stop left RUNNING is FAILED, with a reason, and ``completed_at = updated_at``: it
  ended long ago, outside the ``/ws`` feed's grace, so it never flashes in the bottom bar.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.repositories.stream_cue_coverage import CoverageRead
from backend.repositories.task_progress import ProgressWrite, TaskProgressRepository

__all__ = [
    "FAILED_REASON",
    "LEFTOVER_REASON",
    "CueProgressPorts",
    "CueProgressRows",
    "CueRunProgress",
    "SilentProgress",
    "end_leftover_cue_rows_with",
]

FAILED_REASON = "the cue run stopped on an error; System Logs has the details"
"""M7: why a FAILED cue row failed. The error itself reaches the task's top boundary, which
logs it (``reported_failures``)."""
LEFTOVER_REASON = "the cue worker stopped before the run ended"
"""I1: why a cue row left RUNNING by a crash or a hard stop was failed at the next start."""


class CueRunProgress(Protocol):
    """What a cue run tells its progress sink. Neither call raises nor returns anything."""

    def batch_stored(self, count: int) -> None:
        """A batch committed, storing ``count`` audio (analysed or a fallback row)."""
        ...

    def run_ended(self, *, failed: bool) -> None:
        """The run ended: normally, or (``failed``) in an error that reaches the task."""
        ...


class SilentProgress:
    """No progress row: the default sink."""

    def batch_stored(self, count: int) -> None:
        return None

    def run_ended(self, *, failed: bool) -> None:
        return None


@dataclass(frozen=True)
class CueProgressPorts:
    """Where a run's row goes, how the coverage is counted, the clock, and the row's id.

    ``clock`` is UTC-aware (review I5): the ``/ws`` reaper compares ``updated_at`` with the
    database's ``now()``. ``run_id`` is the row's ``task_id``, new for each run (M12).
    """

    write: ProgressWrite
    coverage: CoverageRead
    clock: Callable[[], datetime]
    run_id: str


class CueProgressRows:
    """One run's progress row (one instance per run)."""

    def __init__(self, ports: CueProgressPorts) -> None:
        self._ports = ports
        self._row: TaskProgress | None = None
        self._no_count = False

    def batch_stored(self, count: int) -> None:
        if count <= 0 or self._no_count:
            return
        row = self._first_row() if self._row is None else self._advanced(self._row, count)
        if row is None:
            self._no_count = True  # I7: this run writes no row
            return
        self._row = row
        self._ports.write(row)

    def run_ended(self, *, failed: bool) -> None:
        if self._row is None:
            return
        now = self._ports.clock()
        status = TaskStatus.FAILED if failed else TaskStatus.COMPLETED
        data = self._row.progress_data
        if failed:
            data = _with_error(data, FAILED_REASON)
        self._row = replace(
            self._row, status=status, progress_data=data, updated_at=now, completed_at=now
        )
        self._ports.write(self._row)

    def _first_row(self) -> TaskProgress | None:
        """The RUNNING row from a fresh count, or None when the count gave nothing."""
        coverage = self._ports.coverage()
        if coverage is None:
            return None
        now = self._ports.clock()
        return TaskProgress(
            task_id=self._ports.run_id,
            task_type=TaskType.CUE_ANALYSIS,
            status=TaskStatus.RUNNING,
            progress_data={"processed": coverage.settled, "total": coverage.analysable},
            started_at=now,
            updated_at=now,
        )

    def _advanced(self, row: TaskProgress, count: int) -> TaskProgress:
        """``row`` with ``count`` more audio settled, never past the total."""
        total: int = row.progress_data["total"]
        processed: int = row.progress_data["processed"]
        data = {"processed": min(total, processed + count), "total": total}
        return replace(row, progress_data=data, updated_at=self._ports.clock())


def _with_error(data: dict[str, Any], reason: str) -> dict[str, Any]:
    return {**data, "error": reason}


def end_leftover_cue_rows_with(repo: TaskProgressRepository) -> None:
    """Fail each cue run row left RUNNING (I1): call it only while no run is live (the cue
    worker's start, ``-w 1``). Its ``updated_at`` stays, and ``completed_at`` is set to it."""
    for row in repo.list_running():
        if row.task_type != TaskType.CUE_ANALYSIS:
            continue
        repo.upsert(
            replace(
                row,
                status=TaskStatus.FAILED,
                progress_data=_with_error(row.progress_data, LEFTOVER_REASON),
                completed_at=row.updated_at,
            )
        )
