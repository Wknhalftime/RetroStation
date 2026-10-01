"""Handing no-cue reports to the cue owner (spec: D79, "The player reports a no-cue song to the
cue owner"; the E2 brief: reports must never slow or fail playback, and the queue is bounded;
E1's lesson: a lasting failure must not flood System Logs)."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from backend.services.streaming.cue_reports import CueReporter


@dataclass
class HeldOwner:
    """A cue owner whose requests wait until ``released`` is set. It records what it was asked
    and the most requests that ever ran at once."""

    asked: list[UUID] = field(default_factory=list)
    released: threading.Event = field(default_factory=threading.Event)
    entered: threading.Event = field(default_factory=threading.Event)
    running: int = 0
    most_running: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, file_id: UUID) -> None:
        with self.lock:
            self.running += 1
            self.most_running = max(self.most_running, self.running)
        self.entered.set()
        try:
            assert self.released.wait(timeout=10), "the test never released the owner"
            self.asked.append(file_id)
        finally:
            with self.lock:
                self.running -= 1


@dataclass
class FlakyOwner:
    """A cue owner that fails or succeeds in the order given."""

    outcomes: list[OSError | None]
    asked: list[UUID] = field(default_factory=list)

    def __call__(self, file_id: UUID) -> None:
        self.asked.append(file_id)
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome


async def inside(owner: HeldOwner) -> None:
    """Wait, on another thread, until a request is inside the owner."""
    assert await asyncio.to_thread(owner.entered.wait, 10)


async def test_the_event_loop_keeps_running_while_a_request_waits() -> None:
    """Never slow playback: the request runs in a worker thread, so this coroutine runs while
    the request is held, and the report reaches the owner once it is released."""
    owner = HeldOwner()
    reporter = CueReporter(owner)
    file_id = uuid4()
    reporter.report(file_id)
    await inside(owner)
    assert owner.asked == []
    owner.released.set()
    await reporter.drained()
    assert owner.asked == [file_id]


async def test_a_failing_request_is_logged_once_and_never_raised() -> None:
    """Never fail playback; E1's lesson: one warning for a lasting failure, then debug."""
    owner = FlakyOwner([OSError("database is locked"), OSError("database is locked")])
    reporter = CueReporter(owner)
    with capture_logs() as logs:
        reporter.report(uuid4())
        reporter.report(uuid4())
        await reporter.drained()
    assert len(owner.asked) == 2
    levels = [e["log_level"] for e in logs if e["event"] == "stream_cue_request_failed"]
    assert levels == ["warning", "debug"]


async def test_a_failure_after_a_success_is_logged_again() -> None:
    """A new failure after the queue recovered is news: warned again."""
    owner = FlakyOwner([OSError("database is locked"), None, OSError("disk full")])
    reporter = CueReporter(owner)
    with capture_logs() as logs:
        for _ in range(3):
            reporter.report(uuid4())
        await reporter.drained()
    levels = [e["log_level"] for e in logs if e["event"] == "stream_cue_request_failed"]
    assert levels == ["warning", "warning"]


async def test_one_request_runs_at_a_time_and_overflow_is_dropped() -> None:
    """D87(c): one request in flight and at most ``pending`` waiting (16 in production); the
    rest are dropped (logged at debug; E1's backlog still reaches those files)."""
    owner = HeldOwner()
    reporter = CueReporter(owner, pending=2)
    files = [uuid4() for _ in range(5)]
    reporter.report(files[0])
    await inside(owner)
    with capture_logs() as logs:
        for file_id in files[1:]:
            reporter.report(file_id)
    owner.released.set()
    await reporter.drained()
    assert owner.asked == files[:3]
    assert owner.most_running == 1
    dropped = [e for e in logs if e["event"] == "stream_cue_request_dropped"]
    assert [(e["log_level"], e["file_id"]) for e in dropped] == [
        ("debug", str(file_id)) for file_id in files[3:]
    ]


def test_the_waiting_limit_must_be_at_least_one() -> None:
    with pytest.raises(ValueError, match="CueReporter.pending"):
        CueReporter(HeldOwner(), pending=0)


async def test_at_most_sixteen_reports_wait() -> None:
    """D87(c): "at most 16 reports wait to be sent, and more are dropped" (the default)."""
    owner = HeldOwner()
    reporter = CueReporter(owner)
    try:
        reporter.report(uuid4())
        await inside(owner)
        with capture_logs() as logs:
            for _ in range(17):
                reporter.report(uuid4())
    finally:
        owner.released.set()
    await reporter.drained()
    assert len(owner.asked) == 1 + 16
    assert len([e for e in logs if e["event"] == "stream_cue_request_dropped"]) == 1
