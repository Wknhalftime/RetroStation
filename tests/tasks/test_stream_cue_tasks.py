"""The cue analysis tasks and their consumer.

Spec: D50 ("its own Huey consumer (its own SQLite file and Procfile line, -w 1)"); D51 (runs
whenever LIQUIDSOAP_PATH is set, whether or not streaming is enabled); D56 (a daily two-strike
prune, 24 h apart); D59 (no progress rows, so task_failure_telemetry is not used); D63 (a
resume every 5 min); review minors: a broken analyser must not flood system_logs every run;
review I3: the date is injected, and registration is read from Huey's registry.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

import backend.tasks.stream_cue_tasks as tasks_module
from backend.domain.library import AudioHash
from backend.domain.streaming import CUE_ANALYSER_VERSION, CueAnalysis
from backend.playout.cue_analysis import AnalyserConfig, BatchAnalysis, CueFile
from backend.playout.errors import AnalyserError
from backend.services.streaming.cue_precompute import CueRunConfig, CueRunPorts
from backend.tasks.cue_huey_app import cue_huey
from backend.tasks.stream_cue_tasks import FailureMemory
from tests.fakes.stream_cues import FakeStreamCueRepository
from tests.services.streaming.test_autocue import VAN_HALEN_POINTS

REPO = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 9, 30, 4, 0, tzinfo=UTC)
TODAY = date(2026, 3, 14)
CUE_TASKS = {
    "backend.tasks.stream_cue_tasks.stream_cue_analysis_task",
    "backend.tasks.stream_cue_tasks.stream_cue_analysis_resume",
    "backend.tasks.stream_cue_tasks.stream_cue_prune_task",
}


class FakeConn:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def settings(tmp_path: Path, *, liquidsoap: bool = True, streaming: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        database_url="postgresql://unused",
        stream_enabled=streaming,
        liquidsoap_path=tmp_path / "liquidsoap.exe" if liquidsoap else None,
        stream_work_dir=tmp_path / "stream",
    )


def wire(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, streaming: bool = True) -> FakeConn:
    """The task with a fake connection, a fixed date and clock, and fresh failure memory."""
    conn = FakeConn()
    configured = settings(tmp_path, streaming=streaming)
    monkeypatch.setattr(tasks_module, "get_settings", lambda: configured)
    monkeypatch.setattr(tasks_module, "connect_sync", lambda *a, **k: nullcontext(conn))
    monkeypatch.setattr(tasks_module, "local_today", lambda: TODAY)
    monkeypatch.setattr(tasks_module, "utc_now", lambda: T0)
    monkeypatch.setattr(tasks_module, "FAILURES", FailureMemory())
    return conn


def test_nothing_runs_without_liquidsoap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D51: LIQUIDSOAP_PATH is the one condition; without it the task does not connect."""
    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings(tmp_path, liquidsoap=False))

    connected: list[object] = []

    def record(*args: object, **kwargs: object) -> object:
        connected.append(args)
        return nullcontext(FakeConn())

    monkeypatch.setattr(tasks_module, "connect_sync", record)
    tasks_module.stream_cue_analysis_task.call_local()
    assert connected == []


def test_analysis_runs_while_streaming_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D51: "whether or not streaming is enabled, so cues are ready before streaming is
    switched on"."""
    runs: list[CueRunConfig] = []
    wire(monkeypatch, tmp_path, streaming=False)
    monkeypatch.setattr(tasks_module, "run_cue_analysis", lambda ports, config: runs.append(config))
    tasks_module.stream_cue_analysis_task.call_local()
    assert len(runs) == 1


def test_the_task_wires_today_and_the_analyser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Composition: today's date places the priority days (D62); the analyser gets the
    allow-listed environment (C8), its own cache folder and the D63 timeouts."""
    seen: list[tuple[CueRunPorts, CueRunConfig]] = []
    analysed: list[tuple[list[CueFile], dict[str, str], AnalyserConfig]] = []

    def fake_analyse(
        files: list[CueFile], base_env: dict[str, str], config: AnalyserConfig
    ) -> BatchAnalysis:
        analysed.append((list(files), dict(base_env), config))
        return BatchAnalysis(metadata={}, stalled=None)

    wire(monkeypatch, tmp_path)
    monkeypatch.setattr(tasks_module, "analyse_batch", fake_analyse)
    monkeypatch.setattr(
        tasks_module, "run_cue_analysis", lambda ports, config: seen.append((ports, config))
    )
    monkeypatch.setenv("AIRWAVE_TOKEN", "secret")
    tasks_module.stream_cue_analysis_task.call_local()
    [(ports, config)] = seen
    assert (config.today, config.batch_size) == (TODAY, 8)
    ports.analyse([CueFile("D:/a.flac", 200_000)])
    [(files, base_env, analyser)] = analysed
    assert files == [CueFile("D:/a.flac", 200_000)]
    assert "AIRWAVE_TOKEN" not in base_env
    assert (analyser.exe, analyser.cache_dir) == (
        tmp_path / "liquidsoap.exe",
        tmp_path / "stream" / "cue-cache",
    )
    assert (analyser.startup_timeout_s, analyser.per_file_timeout_s) == (15.0, 20.0)


def test_the_resume_runs_the_task_in_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """D63: a resume every 5 minutes continues the backlog in bounded runs."""
    ran: list[int] = []
    monkeypatch.setattr(
        tasks_module,
        "stream_cue_analysis_task",
        SimpleNamespace(call_local=lambda: ran.append(1)),
    )
    tasks_module.stream_cue_analysis_resume.call_local()
    assert ran == [1]


def test_the_daily_prune_gives_an_orphan_24_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D56: the first daily prune marks the orphan; the next, 24 h later, deletes it."""
    store = FakeStreamCueRepository()
    orphan = AudioHash.parse("flac-md5:" + "0123456789abcdef" * 2)
    store.upsert(
        CueAnalysis(
            audio_hash=orphan,
            cues=VAN_HALEN_POINTS,
            loudness_lufs=None,
            analysis_failed=False,
            analyser_version=CUE_ANALYSER_VERSION,
        )
    )
    conn = wire(monkeypatch, tmp_path)
    repos = SimpleNamespace(streaming=SimpleNamespace(cues=store))
    monkeypatch.setattr(tasks_module, "RepositoryFactory", lambda c: repos)
    tasks_module.stream_cue_prune_task.call_local()
    assert (store.orphaned, conn.commits) == ({orphan: T0}, 1)
    monkeypatch.setattr(tasks_module, "utc_now", lambda: T0 + timedelta(hours=24))
    tasks_module.stream_cue_prune_task.call_local()
    assert store.analyses == {}


def test_a_failing_run_is_logged_once_and_never_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review minor: a broken analyser fails every 5 minutes; System Logs gets one error per
    distinct failure, and the task never raises (Huey would log every raise too)."""
    wire(monkeypatch, tmp_path)
    failures = iter(
        [AnalyserError("exit code 3"), AnalyserError("exit code 3"), AnalyserError("exit code 9")]
    )

    def fail(ports: object, config: object) -> None:
        raise next(failures)

    monkeypatch.setattr(tasks_module, "run_cue_analysis", fail)
    with capture_logs() as logs:
        for minutes in (0, 5, 10):
            at = T0 + timedelta(minutes=minutes)
            monkeypatch.setattr(tasks_module, "utc_now", lambda at=at: at)
            tasks_module.stream_cue_analysis_task.call_local()
    errors = [
        str(e["error"])
        for e in logs
        if e["event"] == "stream_cue_task_failed" and e["log_level"] == "error"
    ]
    assert len(errors) == 2
    assert "exit code 3" in errors[0] and "exit code 9" in errors[1]


def test_a_failure_still_there_a_day_later_is_logged_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review minor: deduplicated, not silenced; a lasting failure is reported daily."""
    wire(monkeypatch, tmp_path)

    def fail(ports: object, config: object) -> None:
        raise AnalyserError("exit code 3")

    monkeypatch.setattr(tasks_module, "run_cue_analysis", fail)
    with capture_logs() as logs:
        for hours in (0, 24):
            at = T0 + timedelta(hours=hours)
            monkeypatch.setattr(tasks_module, "utc_now", lambda at=at: at)
            tasks_module.stream_cue_analysis_task.call_local()
    levels = [e["log_level"] for e in logs if e["event"] == "stream_cue_task_failed"]
    assert levels == ["error", "error"]


def test_the_resume_runs_every_5_minutes_and_the_prune_once_a_day() -> None:
    """D63: a resume every 5 minutes; D56: one prune a day. Huey schedules in UTC, so the
    counts over one day are pinned, not the hour."""
    day = datetime(2026, 9, 30)
    fired: Counter[str] = Counter()
    for minute in range(24 * 60):
        for task in cue_huey.read_periodic(day + timedelta(minutes=minute)):
            fired[task.name] += 1
    assert fired == {"stream_cue_analysis_resume": 288, "stream_cue_prune_task": 1}


PROBE = """
import json
import backend.tasks.cue_huey_app as cues
cue_tasks = sorted(cues.cue_huey._registry._registry)
import backend.tasks.huey_app as library
print(json.dumps({
    "cue": cue_tasks,
    "library": sorted(library.huey._registry._registry),
    "files": [cues.cue_huey.storage.filename, library.huey.storage.filename],
}))
"""


@pytest.mark.slow
def test_cue_analysis_has_its_own_consumer() -> None:
    """D50: its own Huey instance and SQLite file. Importing only the consumer's module
    registers every cue task on it, and none on the library worker."""
    probe = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    seen = json.loads(probe.stdout.strip().splitlines()[-1])
    assert set(seen["cue"]) >= CUE_TASKS
    assert not CUE_TASKS & set(seen["library"])
    assert seen["files"][0] != seen["files"][1]


def test_the_procfile_runs_the_cue_consumer_with_one_worker() -> None:
    """D50: "its own ... Procfile line, -w 1"."""
    processes = dict(
        line.split(":", 1)
        for line in (REPO / "Procfile").read_text(encoding="utf-8").splitlines()
        if ":" in line
    )
    assert processes["cues"].split() == [
        "uv",
        "run",
        "python",
        "-m",
        "huey.bin.huey_consumer",
        "backend.tasks.cue_huey_app.cue_huey",
        "-w",
        "1",
    ]
