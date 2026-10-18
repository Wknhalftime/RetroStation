"""In-memory fake for the matching run's progress row.

``FakeMatchingRunRepository`` keeps the rows as ``TaskProgress`` values, with the arithmetic
the Pg adapter does in SQL (spec 2026-10-17 "Matching run progress"):

- ``refresh``: ``total' = max(total, processed + pending)``, ``processed' = total' - pending``;
- ``finish_stage``: as ``refresh`` (``pending`` None keeps ``processed``), then one stage fewer,
  one more failed stage when ``failed``; at no stage left the row is COMPLETED, with
  ``warning = "matching_stages_failed"`` when any stage failed;
- a RUNNING or FAILED row (the ``/ws`` reaper's) is brought back to RUNNING, ``completed_at``
  cleared; a COMPLETED row and an unknown id are left alone.

``calls`` records every call in order, for the task tests.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.repositories.matching_run import MatchingRunRepository

STAGES_FAILED_WARNING = "matching_stages_failed"


class FakeMatchingRunRepository(MatchingRunRepository):
    def __init__(self) -> None:
        self.rows: dict[str, TaskProgress] = {}
        self.calls: list[tuple[Any, ...]] = []

    # Port -------------------------------------------------------------------------------

    def open(self, run_id: str, *, total: int, stages: int) -> None:
        self.calls.append(("open", run_id, total, stages))
        now = datetime.now(UTC)
        self.rows[run_id] = TaskProgress(
            task_id=run_id,
            task_type=TaskType.MATCHING,
            status=TaskStatus.RUNNING,
            progress_data={
                "processed": 0,
                "total": total,
                "stages_left": stages,
                "stages_failed": 0,
            },
            started_at=now,
            updated_at=now,
        )

    def refresh(self, run_id: str, *, pending: int) -> None:
        self.calls.append(("refresh", run_id, pending))
        row = self._live(run_id)
        if row is None:
            return
        self.rows[run_id] = self._revived(row, _counted(row.progress_data, pending))

    def finish_stage(self, run_id: str, *, pending: int | None, failed: bool) -> None:
        self.calls.append(("finish_stage", run_id, pending, failed))
        row = self._live(run_id)
        if row is None:
            return
        data = _counted(row.progress_data, pending)
        data["stages_left"] = int(data["stages_left"]) - 1
        data["stages_failed"] = int(data["stages_failed"]) + (1 if failed else 0)
        if data["stages_left"] > 0:
            self.rows[run_id] = self._revived(row, data)
            return
        if data["stages_failed"] > 0:
            data["warning"] = STAGES_FAILED_WARNING
        now = datetime.now(UTC)
        self.rows[run_id] = replace(
            row,
            status=TaskStatus.COMPLETED,
            progress_data=data,
            updated_at=now,
            completed_at=now,
        )

    # Test helpers -----------------------------------------------------------------------

    def reap(self, run_id: str) -> datetime:
        """What the ``/ws`` stale reaper does to a row left 10 min without an update.
        Returns the row's (old) ``updated_at``."""
        row = self.rows[run_id]
        old = row.updated_at - timedelta(minutes=20)
        self.rows[run_id] = replace(
            row, status=TaskStatus.FAILED, updated_at=old, completed_at=datetime.now(UTC)
        )
        return old

    def finished(self, run_id: str) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == "finish_stage" and c[1] == run_id]

    def refreshed(self, run_id: str) -> list[int]:
        return [c[2] for c in self.calls if c[0] == "refresh" and c[1] == run_id]

    # Internals --------------------------------------------------------------------------

    def _live(self, run_id: str) -> TaskProgress | None:
        row = self.rows.get(run_id)
        if row is None or row.status not in (TaskStatus.RUNNING, TaskStatus.FAILED):
            return None
        return row

    @staticmethod
    def _revived(row: TaskProgress, data: dict[str, Any]) -> TaskProgress:
        return replace(
            row,
            status=TaskStatus.RUNNING,
            progress_data=data,
            updated_at=datetime.now(UTC),
            completed_at=None,
        )


def _counted(progress_data: dict[str, Any], pending: int | None) -> dict[str, Any]:
    data = dict(progress_data)
    if pending is None:
        return data
    total = max(int(data["total"]), int(data["processed"]) + pending)
    data["total"] = total
    data["processed"] = total - pending
    return data
