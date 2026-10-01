"""The cue consumer's request task and the request that queues it (spec: D79, the report
reaches the cue owner; D50, its own consumer with one worker, so a request never runs beside
a run; D51; D87(a): priority above the resume and a one-hour expiry; E1 lesson: one error
per distinct failure a day, never re-raised)."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from huey.api import Task  # type: ignore[import-untyped]
from structlog.testing import capture_logs

import backend.tasks.stream_cue_tasks as tasks_module
from backend.domain.streaming import StreamTiming
from backend.playout.cue_analysis import AnalyserConfig, BatchAnalysis, CueFile
from backend.playout.errors import AnalyserError
from backend.services.streaming.cue_precompute import CueRunPorts
from backend.tasks.cue_huey_app import cue_huey
from tests.tasks.test_stream_cue_tasks import FakeConn, settings, wire


def test_a_request_does_nothing_without_liquidsoap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D51: without LIQUIDSOAP_PATH nothing analyses, so the task does not even connect."""
    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings(tmp_path, liquidsoap=False))
    connected: list[object] = []

    def record(*args: object, **kwargs: object) -> AbstractContextManager[FakeConn]:
        connected.append(args)
        return nullcontext(FakeConn())

    monkeypatch.setattr(tasks_module, "connect_sync", record)
    tasks_module.stream_cue_request_task.call_local(str(uuid4()))
    assert connected == []


def test_a_request_analyses_the_reported_file_with_the_cue_workers_analyser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Composition: the reported file id, the default fades for a fallback row, the
    analyser E1's run uses (its own cache folder), and the task's connection's commit."""
    seen: list[tuple[CueRunPorts, StreamTiming, UUID]] = []
    analysers: list[AnalyserConfig] = []

    def fake_analyse(
        files: list[CueFile], base_env: dict[str, str], config: AnalyserConfig
    ) -> BatchAnalysis:
        analysers.append(config)
        return BatchAnalysis(metadata={}, stalled=None)

    conn = wire(monkeypatch, tmp_path)
    monkeypatch.setattr(tasks_module, "analyse_batch", fake_analyse)
    monkeypatch.setattr(
        tasks_module,
        "analyse_reported",
        lambda ports, timing, file_id: seen.append((ports, timing, file_id)),
    )
    file_id = uuid4()
    tasks_module.stream_cue_request_task.call_local(str(file_id))
    [(ports, timing, reported)] = seen
    assert (reported, timing) == (file_id, StreamTiming())
    ports.analyse([CueFile("D:/a.flac", 200_000)])
    ports.commit()
    [analyser] = analysers
    assert (analyser.exe, analyser.cache_dir) == (
        tmp_path / "liquidsoap.exe",
        tmp_path / "stream" / "cue-cache",
    )
    assert conn.commits == 1


def test_a_failing_request_is_logged_once_and_never_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1's boundary: one error per distinct failure a day; Huey never sees a raise."""
    wire(monkeypatch, tmp_path)

    def fail(ports: object, timing: object, file_id: object) -> None:
        raise AnalyserError("cue analyser exit code 3: no such file")

    monkeypatch.setattr(tasks_module, "analyse_reported", fail)
    with capture_logs() as logs:
        for _ in range(2):
            tasks_module.stream_cue_request_task.call_local(str(uuid4()))
    levels = [e["log_level"] for e in logs if e["event"] == "stream_cue_task_failed"]
    assert levels == ["error", "debug"]


def test_a_report_is_queued_on_the_cue_consumer_ahead_of_the_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D79's crossing (D87(a)): a Huey task on the cue consumer (D50), with a priority above
    the 5-minute resume's, which expires after an hour if the consumer is not running."""
    queued: list[Task] = []
    monkeypatch.setattr(cue_huey, "enqueue", queued.append)
    file_id = uuid4()
    tasks_module.request_cue_analysis(file_id)
    [task] = queued
    assert (task.name, task.args) == ("stream_cue_request_task", (str(file_id),))
    resume_priority = tasks_module.stream_cue_analysis_resume.task_class.default_priority or 0
    assert task.priority == tasks_module.REQUEST_PRIORITY > resume_priority
    assert task.expires == tasks_module.REQUEST_EXPIRES_S == 3600
    assert "backend.tasks.stream_cue_tasks.stream_cue_request_task" in cue_huey._registry._registry
