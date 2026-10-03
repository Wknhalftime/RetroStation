"""The shared progress writer (PR G2, Task 7; traceability X: T7.12-T7.15, T7.17).

Requirements:
- D94: telemetry writes do not wait for the disk: the writer's connection sets
  ``synchronous_commit = off``, with a 1 s statement and a 0.5 s lock bound (design note 10);
- D90 ("System Logs are not flooded"): a failed write is dropped, never raised; one warning
  per outage, one line on recovery, nothing per successful write;
- design note 10: a dropped connection is replaced on the next write; I6: the write is one
  statement (a write-only upsert, no follow-up read);
- M9: once the final row is written, a late RUNNING write (a cancelled tick that lands late)
  is dropped, so it cannot reopen the row; design note 10: the writes are made "under a lock",
  so a tick still writing in the worker thread and the final row never interleave;
- H3/H7: specific exceptions at their layer; telemetry never changes the caller's outcome.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

from backend.db.progress_writer import ProgressWriter, writer_options
from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress

T0 = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
STATUSES = {status.value for status in TaskStatus}
LOUD = {"warning", "error", "critical"}
NOT_DEBUG = {"info", "warning", "error", "critical"}
THREAD_GUARD_S = 10.0
"""Fails a test whose thread never gets going; never used to order anything."""
OVERLAP_WINDOW_S = 0.5
"""How long the first statement stays open for a second one to arrive. Correct code passes
whatever the window; it only bounds how long an unlocked writer has to show its overlap."""


def row(status: TaskStatus, seconds: int = 0) -> TaskProgress:
    now = T0 + timedelta(seconds=seconds)
    return TaskProgress(
        task_id="stream_resources",
        task_type=TaskType.STREAM_RESOURCES,
        status=status,
        progress_data={"open_streams": 1},
        started_at=T0,
        updated_at=now,
        completed_at=None if status == TaskStatus.RUNNING else now,
    )


def values(params: object) -> Iterable[object]:
    if isinstance(params, dict):
        return params.values()
    if isinstance(params, (list, tuple)):
        return params
    return ()


class Conn:
    """A connection whose statements succeed, or raise ``fail``."""

    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.executed: list[tuple[str, object]] = []
        self.closed = False

    def execute(self, query: object, params: object = None) -> Conn:
        if self.fail is not None:
            raise self.fail
        self.executed.append((str(query), params))
        return self

    def commit(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def statuses(self) -> list[object]:
        return [v for _, params in self.executed for v in values(params) if v in STATUSES]


class Connects:
    """A scripted ``connect``: each call takes the next connection, or raises the next error."""

    def __init__(self, *outcomes: Conn | BaseException) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def loud(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["log_level"] in LOUD]


@pytest.mark.parametrize(
    "error",
    [
        psycopg.OperationalError("server closed the connection unexpectedly"),
        psycopg.InterfaceError("the connection is closed"),
        OSError("network is unreachable"),
    ],
    ids=["operational", "interface", "os-error"],
)
def test_a_write_that_fails_is_dropped_and_logged_once_per_outage(error: Exception) -> None:
    # T7.12 (design note 10, D90, H7): the database down (connect fails) or a broken
    # connection (the statement fails): dropped, never raised; one warning per outage.
    good = Conn()
    writer = ProgressWriter(Connects(error, Conn(fail=error), error, good, error))
    with capture_logs() as logs:
        for second in range(3):
            writer.write(row(TaskStatus.RUNNING, second))
        writer.write(row(TaskStatus.RUNNING, 3))
        assert good.statuses() == ["running"]
        good.fail = error
        writer.write(row(TaskStatus.RUNNING, 4))
        writer.write(row(TaskStatus.RUNNING, 5))
    assert len(loud(logs)) == 2


def test_after_an_outage_the_writer_reconnects_and_logs_recovery_once() -> None:
    # T7.13 (design note 10; I6): the broken connection is closed and replaced on the next
    # write; recovery is said once; later writes say nothing and are one statement each.
    broken = Conn(fail=psycopg.OperationalError("terminating connection"))
    good = Conn()
    connects = Connects(broken, good)
    writer = ProgressWriter(connects)
    with capture_logs() as outage:
        writer.write(row(TaskStatus.RUNNING, 0))
    assert broken.closed
    with capture_logs() as recovery:
        writer.write(row(TaskStatus.RUNNING, 2))
    with capture_logs() as steady:
        writer.write(row(TaskStatus.RUNNING, 4))
        writer.write(row(TaskStatus.RUNNING, 6))
    assert len(loud(outage)) == 1
    assert len([e for e in recovery if e["log_level"] in NOT_DEBUG]) == 1
    assert [e for e in steady if e["log_level"] in NOT_DEBUG] == []
    assert connects.calls == 2
    assert len(good.executed) == 3
    writer.close()
    assert good.closed


def test_after_completion_a_late_running_write_is_dropped() -> None:
    # T7.14 (M9): the final row is written; a RUNNING write that lands after it is dropped.
    conn = Conn()
    writer = ProgressWriter(Connects(conn))
    writer.write(row(TaskStatus.RUNNING, 0))
    writer.complete(row(TaskStatus.COMPLETED, 2))
    writer.write(row(TaskStatus.RUNNING, 1))
    assert conn.statuses() == ["running", "completed"]


def test_the_writer_connection_does_not_wait_for_the_disk() -> None:
    # T7.15 (D94; design note 10): the options the writer's connection is opened with.
    settings = dict(re.findall(r"-c\s*([a-z_]+)\s*=\s*(\S+)", writer_options()))
    assert settings["synchronous_commit"] == "off"
    assert settings["statement_timeout"] == "1000"
    assert settings["lock_timeout"] == "500"


class HeldConn(Conn):
    """A connection whose first statement stays open until another statement starts, or the
    window ends; a statement is recorded when it ends."""

    def __init__(self) -> None:
        super().__init__()
        self.inside = 0
        self.overlapped = False
        self.first_in = threading.Event()
        self.second_in = threading.Event()
        self._guard = threading.Lock()

    def execute(self, query: object, params: object = None) -> Conn:
        with self._guard:
            self.inside += 1
            first = not self.first_in.is_set()
            if not first:
                self.overlapped = self.overlapped or self.inside > 1
                self.second_in.set()
            self.first_in.set()
        if first:
            self.second_in.wait(OVERLAP_WINDOW_S)
        with self._guard:
            self.inside -= 1
            self.executed.append((str(query), params))
        return self


def test_writes_from_two_threads_never_overlap() -> None:
    # T7.17 (design note 10 "under a lock"; M9; audit note 3): a tick's RUNNING write is
    # still open in the worker thread when the final row is written from another thread. The
    # final row waits for it, so the two never interleave and the last row is COMPLETED.
    conn = HeldConn()
    writer = ProgressWriter(Connects(conn))
    tick = threading.Thread(target=writer.write, args=(row(TaskStatus.RUNNING, 0),))
    tick.start()
    assert conn.first_in.wait(THREAD_GUARD_S)
    final = threading.Thread(target=writer.complete, args=(row(TaskStatus.COMPLETED, 2),))
    final.start()
    tick.join(THREAD_GUARD_S)
    final.join(THREAD_GUARD_S)
    assert not tick.is_alive() and not final.is_alive()
    assert not conn.overlapped
    assert conn.statuses() == ["running", "completed"]
