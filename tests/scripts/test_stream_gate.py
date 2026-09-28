"""scripts/stream_gate.py: the engine gate verdict. Pure, so it runs on CI (Linux)."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from backend.playout.assets import ensure_stream_assets

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("stream_gate")


def test_soak_clean_run_has_no_problems(gate: ModuleType) -> None:
    assert gate.soak_problems(started=[0, 1, 3, 4], failed=[2], broken={2}) == []


def test_soak_detects_repeat_and_skip(gate: ModuleType) -> None:
    problems = gate.soak_problems(started=[0, 1, 1, 4], failed=[], broken=set())
    assert any("repeated" in p for p in problems)
    assert any("skipped [2, 3]" in p for p in problems)


def test_soak_detects_out_of_order_start(gate: ModuleType) -> None:
    assert gate.soak_problems(started=[0, 2, 1], failed=[], broken=set()) != []


def test_soak_detects_unreported_broken_file(gate: ModuleType) -> None:
    assert gate.soak_problems(started=[0, 2], failed=[], broken={1}) != []
    assert gate.soak_problems(started=[0, 2], failed=[1], broken={1}) == []


def test_soak_ignores_broken_items_after_the_run_ended(gate: ModuleType) -> None:
    assert gate.soak_problems(started=[0, 1], failed=[], broken={9}) == []


def test_longest_gap(gate: ModuleType) -> None:
    assert gate.longest_gap_s([10.0, 20.0, 45.0, 50.0]) == 25.0
    assert gate.longest_gap_s([3.0]) == 0.0


def test_silences_parsed_from_ffmpeg(gate: ModuleType) -> None:
    stderr = (
        "[silencedetect @ 0x1] silence_end: 12.5 | silence_duration: 1.25\n"
        "[silencedetect @ 0x1] silence_end: 40 | silence_duration: 2\n"
    )
    assert gate.silences_s(stderr) == [1.25, 2.0]


def test_merge_reports_rejects_duplicate_keys(gate: ModuleType) -> None:
    assert gate.merge_reports([{"a": 1}, {"b": 2}]) == {"a": 1, "b": 2}
    with pytest.raises(ValueError, match="'a'"):
        gate.merge_reports([{"a": 1}, {"a": 2}])


GOOD: dict[str, object] = {
    "latency_s": [0.8, 1.2],
    "cpu_percent": [9.0, 11.0],
    "rss_mb": [80.0, 85.0],
    "soak_problems": [],
    "silences_s": [],
    "start_times_s": [1.0, 30.0, 70.0],
    "soak_end_s": 120.0,
    "max_span_s": 40.0,
    "items_started": 3,
    "soak_planned_s": 118.0,
    "soak_streamed_s": 118.5,
    "decode_exit_code": 0,
}

# One breach each; the freeze margin is max_span_s + 30 s = 70 s.
BREACHES: dict[str, dict[str, object]] = {
    "first audio": {"latency_s": [0.8, 1.6]},
    "cpu": {"cpu_percent": [16.0]},
    "rss": {"rss_mb": [151.0]},
    "soak problems": {"soak_problems": ["x"]},
    "silence": {"silences_s": [0.6, 1.5]},
    "freeze between starts": {"start_times_s": [1.0, 72.0], "soak_end_s": 100.0},
    "freeze after last start": {"soak_end_s": 141.0},
    "stream cut short": {"soak_streamed_s": 115.9},
    "decode error": {"decode_exit_code": 1},
    "nothing started": {"start_times_s": [], "items_started": 0, "soak_end_s": 60.0},
}


def test_gate_passes_good_report(gate: ModuleType) -> None:
    assert gate.gate_failures(GOOD, gate.GateLimits()) == []


@pytest.mark.parametrize("breach", BREACHES.values(), ids=BREACHES.keys())
def test_gate_reports_each_breach_once(gate: ModuleType, breach: dict[str, object]) -> None:
    assert len(gate.gate_failures(GOOD | breach, gate.GateLimits())) == 1


def test_gate_reports_all_breaches_together(gate: ModuleType) -> None:
    bad = GOOD | {
        "latency_s": [1.6],
        "cpu_percent": [16.0],
        "rss_mb": [151.0],
        "soak_problems": ["x"],
        "silences_s": [1.5],
        "soak_end_s": 141.0,
        "soak_streamed_s": 100.0,
        "decode_exit_code": 1,
    }
    assert len(gate.gate_failures(bad, gate.GateLimits())) == 8


def test_gate_does_not_gate_cache_build_time(gate: ModuleType) -> None:
    assert gate.gate_failures(GOOD | {"cache_build_s": 30.0}, gate.GateLimits()) == []


def test_gate_fails_when_a_stage_is_missing(gate: ModuleType) -> None:
    failures = gate.gate_failures(
        {k: v for k, v in GOOD.items() if k != "rss_mb"}, gate.GateLimits()
    )
    assert failures == ["missing measurement: rss_mb"]


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_silence_filter_counts_the_filler_as_a_gap(gate: ModuleType, tmp_path: Path) -> None:
    filler = ensure_stream_assets("ffmpeg", tmp_path).filler
    tone = "aevalsrc=0.25*sin(500*2*PI*t):s=44100:d=3,aformat=channel_layouts=stereo"
    graph = f"[0][1][2]concat=n=3:v=0:a=1,{gate.SILENCE_FILTER}"
    stderr = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-f",
            "lavfi",
            "-i",
            tone,
            "-i",
            str(filler),
            "-f",
            "lavfi",
            "-i",
            tone,
            "-filter_complex",
            graph,
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    ).stderr
    assert gate.silences_s(stderr) == [pytest.approx(2.0, abs=0.1)]
