"""The composition root wires the stream service's no-cue reports to the cue consumer, and
flushes the waiting ones at shutdown (spec: D79; architecture rule: main is the only wiring
site; D87(a): a Huey task on the cue consumer; D87(b) (review I3): stop_streaming waits at
most 2 s for the waiting reports)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from huey.api import Task  # type: ignore[import-untyped]
from structlog.testing import capture_logs

import backend.main as main_module
from backend.main import StreamingRuntime, build_stream_ports, stop_streaming
from backend.playout.liquidsoap_process import RunningEngine, SessionEndpoint
from backend.services.streaming.cue_reports import CueReporter
from backend.tasks.cue_huey_app import cue_huey
from tests.services.streaming.helpers import make_rig
from tests.services.streaming.test_cue_reports import HeldOwner, inside


async def no_engine(endpoint: SessionEndpoint) -> RunningEngine:
    raise AssertionError("no engine is started here")


def runtime(
    tmp_path: Path, owner: HeldOwner, reporter: CueReporter, closed: list[list[UUID]]
) -> StreamingRuntime:
    """A streaming runtime with no sessions; closing its job records what the owner had been
    sent by then."""
    return StreamingRuntime(
        service=make_rig(tmp_path).service,
        close_job=lambda: closed.append(list(owner.asked)),
        watchdog=asyncio.ensure_future(asyncio.Event().wait()),
        cue_reports=reporter,
    )


async def test_no_cue_reports_reach_the_cue_consumers_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued: list[Task] = []
    monkeypatch.setattr(cue_huey, "enqueue", queued.append)
    ports = build_stream_ports(SimpleNamespace(database_url="postgresql://unused"), no_engine)
    file_id = uuid4()
    ports.cue_reports.report(file_id)
    await ports.cue_reports.drained()
    assert [(t.name, t.args) for t in queued] == [("stream_cue_request_task", (str(file_id),))]


async def test_stopping_streaming_sends_the_reports_still_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I3 and D87(b): reports accepted before shutdown reach the cue owner before
    stop_streaming returns. Whether that is before or after the engine job closes is left
    open (audit SF-5)."""
    monkeypatch.setattr(main_module, "REPORT_FLUSH_S", 30.0)
    owner = HeldOwner()
    reporter = CueReporter(owner)
    files = [uuid4(), uuid4()]
    closed: list[list[UUID]] = []
    try:
        reporter.report(files[0])
        await inside(owner)
        reporter.report(files[1])
        stopping = asyncio.ensure_future(stop_streaming(runtime(tmp_path, owner, reporter, closed)))
        done, _ = await asyncio.wait({stopping}, timeout=1.0)
        assert stopping not in done  # it is waiting for the held report
    finally:
        owner.released.set()  # an assertion above still raises, after the release
    await stopping
    assert owner.asked == files  # sent before stop_streaming returned
    assert len(closed) == 1


async def test_stopping_streaming_waits_for_reports_only_so_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I3 and D87(b): the flush is bounded; a stuck report never holds shutdown, and the
    reports left behind are logged (R23)."""
    monkeypatch.setattr(main_module, "REPORT_FLUSH_S", 0.05)
    owner = HeldOwner()
    reporter = CueReporter(owner)
    closed: list[list[UUID]] = []
    try:
        reporter.report(uuid4())
        await inside(owner)
        with capture_logs() as logs:
            await stop_streaming(runtime(tmp_path, owner, reporter, closed))
        assert closed == [[]]  # the job closed while the report was still held
        assert owner.running == 1  # and shutdown did not wait out the held report
        unflushed = [e["log_level"] for e in logs if e["event"] == "stream_cue_reports_unflushed"]
        assert unflushed == ["warning"]
    finally:
        owner.released.set()


def test_shutdown_flushes_reports_for_at_most_two_seconds() -> None:
    """D87(b): "pending reports are flushed for at most 2 s, then dropped"."""
    assert main_module.REPORT_FLUSH_S == 2.0
