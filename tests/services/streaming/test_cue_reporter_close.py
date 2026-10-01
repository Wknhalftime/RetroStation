"""What ``CueReporter.close()`` promises (final review I1, carried from Task 6a; D87(b)): the
reports still waiting are dropped, not sent into a closed worker and logged as failures; a
report after close is ignored; and a request still running never holds the app's exit."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from backend.main import StreamingRuntime, stop_streaming
from backend.services.streaming.cue_reports import CueReporter
from backend.services.streaming.service import StreamService
from tests.services.streaming.test_cue_reports import HeldOwner, inside


async def test_reports_waiting_at_close_are_dropped_without_a_failure_warning() -> None:
    owner = HeldOwner()
    reporter = CueReporter(owner)
    held, *waiting = uuid4(), uuid4(), uuid4()
    with capture_logs() as logs:
        try:
            reporter.report(held)
            await inside(owner)
            for file_id in waiting:
                reporter.report(file_id)
            reporter.close()
        finally:
            owner.released.set()
        await reporter.drained()
    assert owner.asked == [held]  # the running request completes; the waiting ones are dropped
    assert [e for e in logs if e["event"] == "stream_cue_request_failed"] == []


async def test_a_report_after_close_is_ignored() -> None:
    owner = HeldOwner()
    owner.released.set()
    reporter = CueReporter(owner)
    reporter.close()
    with capture_logs() as logs:
        reporter.report(uuid4())
        await reporter.drained()
    assert owner.asked == []
    assert [e["log_level"] for e in logs] == ["debug"]


async def test_a_running_request_never_holds_the_apps_exit() -> None:
    """The interpreter joins non-daemon threads (a ``ThreadPoolExecutor``'s workers too) at
    exit; the reporter's worker is a daemon, so a hung request cannot delay it."""
    on_daemon: list[bool] = []

    def owner(file_id: object) -> None:
        on_daemon.append(threading.current_thread().daemon)

    reporter = CueReporter(owner)
    reporter.report(uuid4())
    await reporter.drained()
    reporter.close()
    assert on_daemon == [True]


async def test_stop_streaming_closes_the_reporter_when_closing_the_sessions_raises() -> None:
    """Re-review N1: a failing ``close_all`` still propagates, and the flush, the reporter's
    close and the job's close all still run."""
    owner = HeldOwner()
    owner.released.set()
    reporter = CueReporter(owner)
    sent, after = uuid4(), uuid4()
    reporter.report(sent)
    jobs_closed: list[list[UUID]] = []

    def close_all() -> None:
        raise RuntimeError("close_all failed")

    runtime = StreamingRuntime(
        service=cast(StreamService, SimpleNamespace(close_all=close_all)),
        close_job=lambda: jobs_closed.append(list(owner.asked)),
        watchdog=asyncio.ensure_future(asyncio.Event().wait()),
        cue_reports=reporter,
    )
    with pytest.raises(RuntimeError, match="close_all failed"):
        await stop_streaming(runtime)
    assert jobs_closed == [[sent]]  # flushed before the job closed
    reporter.report(after)  # closed: ignored
    await reporter.drained()
    assert owner.asked == [sent]
