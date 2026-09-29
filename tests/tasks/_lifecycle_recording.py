"""Shared recorders for AUD-025 task-lifecycle characterisation tests.

Wraps the existing `FakeTaskProgressRepository` / `FakeSystemLogRepository`
fakes (tests/fakes/) so every `upsert` / `create` call lands in one ordered,
interleaved `events` list — call order across the two repos matters for
these tests (e.g. "RUNNING upsert happens before the started SystemLog"),
and the two fakes don't share a clock precise enough to reconstruct that
order after the fact.

`normalize_events` turns that list into a plain, snapshot-stable structure:
task_id / trace_id are asserted internally-consistent then dropped,
timestamps are dropped, and a `traceback` detail key is asserted non-empty
then replaced with a fixed placeholder so the snapshot doesn't pin exact
frame text.
"""

from __future__ import annotations

from typing import Any

from backend.domain.system import SystemLog, TaskProgress
from tests.fakes.system_logs import FakeSystemLogRepository
from tests.fakes.task_progress import FakeTaskProgressRepository

LifecycleEvent = tuple[str, Any]


class OrderedProgressRepo(FakeTaskProgressRepository):
    """Records every `upsert` into a shared, cross-repo-ordered event list."""

    def __init__(self, events: list[LifecycleEvent]) -> None:
        super().__init__()
        self._events = events

    def upsert(self, task: TaskProgress) -> TaskProgress:
        result = super().upsert(task)
        self._events.append(("progress", task))
        return result


class OrderedSystemLogRepo(FakeSystemLogRepository):
    """Records every `create` into a shared, cross-repo-ordered event list."""

    def __init__(self, events: list[LifecycleEvent]) -> None:
        super().__init__()
        self._events = events

    def create(self, log: SystemLog) -> None:
        super().create(log)
        self._events.append(("log", log))


class FlakyProgressRepo(OrderedProgressRepo):
    """Raises on the given 1-indexed `upsert` call number; passes through
    every other call to the real fake so business state stays consistent.
    """

    def __init__(self, events: list[LifecycleEvent], *, fail_on_call: int) -> None:
        super().__init__(events)
        self._fail_on_call = fail_on_call
        self.upsert_calls = 0

    def upsert(self, task: TaskProgress) -> TaskProgress:
        self.upsert_calls += 1
        if self.upsert_calls == self._fail_on_call:
            raise RuntimeError("simulated telemetry write failure")
        return super().upsert(task)


def normalize_events(events: list[LifecycleEvent]) -> list[dict[str, Any]]:
    """Convert recorded (TaskProgress | SystemLog) events into a snapshot-
    stable structure: single consistent task_id/trace_id asserted then
    dropped, timestamps dropped, traceback presence asserted then replaced.
    """
    task_ids = {e.task_id for kind, e in events if kind == "progress"}
    task_ids |= {e.trace_id for kind, e in events if kind == "log" and e.trace_id is not None}
    assert len(task_ids) <= 1, f"expected a single task_id/trace_id across the run, got {task_ids}"

    normalized: list[dict[str, Any]] = []
    for kind, payload in events:
        if kind == "progress":
            assert isinstance(payload, TaskProgress)
            normalized.append(
                {
                    "kind": "progress",
                    "status": payload.status.value,
                    "task_type": payload.task_type.value,
                    "progress_data": payload.progress_data,
                    "completed_at_is_set": payload.completed_at is not None,
                }
            )
        else:
            assert isinstance(payload, SystemLog)
            details = dict(payload.details) if payload.details is not None else None
            if details is not None and "traceback" in details:
                traceback_text = details["traceback"]
                assert isinstance(traceback_text, str) and traceback_text.strip(), (
                    "FAILED SystemLog must carry a non-empty traceback"
                )
                details = {**details, "traceback": "<TRACEBACK>"}
            normalized.append(
                {
                    "kind": "log",
                    "category": payload.category.value,
                    "level": payload.level.value,
                    "message": payload.message,
                    "details": details,
                }
            )
    return normalized
