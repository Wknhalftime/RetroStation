"""Engine gate verdict for tune-in streaming (spec: Delivery, PR A). Pure; any OS."""

from __future__ import annotations

import re
from dataclasses import dataclass

REQUIRED = (
    "latency_s",
    "cpu_percent",
    "rss_mb",
    "soak_problems",
    "silences_s",
    "start_times_s",
    "soak_end_s",
    "max_span_s",
    "items_started",
    "soak_planned_s",
    "soak_streamed_s",
    "decode_exit_code",
)
# -30 dB, not -50: the filler loop (about -57 dBFS RMS) must count as a gap.
SILENCE_FILTER = "silencedetect=noise=-30dB:d=0.5"
_SILENCE = re.compile(r"silence_duration: ([0-9.]+)")


@dataclass(frozen=True)
class GateLimits:
    first_audio_s: float = 1.5
    cpu_percent: float = 15.0
    rss_mb: float = 150.0
    max_silence_s: float = 1.0
    freeze_margin_s: float = 30.0
    short_stream_s: float = 2.0


def soak_problems(started: list[int], failed: list[int], broken: set[int]) -> list[str]:
    """Order, repeat, skip and failure-report checks on the soak's started/failed seqs."""
    problems: list[str] = []
    if len(started) != len(set(started)):
        problems.append(f"started items repeated: {started}")
    if started != sorted(started):
        problems.append(f"started out of order: {started}")
    if started:
        reached = range(max(started) + 1)
        skipped = sorted(set(reached) - set(started) - broken)
        if skipped:
            problems.append(f"skipped {skipped}")
        unreported = sorted(seq for seq in broken if seq in reached and seq not in failed)
        if unreported:
            problems.append(f"broken items not reported as failed: {unreported}")
    return problems


def longest_gap_s(times: list[float]) -> float:
    return max((b - a for a, b in zip(times, times[1:], strict=False)), default=0.0)


def silences_s(ffmpeg_stderr: str) -> list[float]:
    return [float(match) for match in _SILENCE.findall(ffmpeg_stderr)]


def merge_reports(reports: list[dict[str, object]]) -> dict[str, object]:
    merged: dict[str, object] = {}
    for report in reports:
        clash = sorted(set(merged) & set(report))
        if clash:
            raise ValueError(f"stage reports overlap on {clash[0]!r}")
        merged |= report
    return merged


def _floats(report: dict[str, object], key: str) -> list[float]:
    value = report[key]
    if isinstance(value, list):
        return [float(v) for v in value]
    raise ValueError(f"report[{key!r}] must be a list, got {type(value).__name__}")


def _number(report: dict[str, object], key: str) -> float:
    value = report[key]
    if isinstance(value, int | float):
        return float(value)
    raise ValueError(f"report[{key!r}] must be a number, got {type(value).__name__}")


def _resource_failures(report: dict[str, object], limits: GateLimits) -> list[str]:
    failures: list[str] = []
    latency = max(_floats(report, "latency_s"), default=0.0)
    if latency > limits.first_audio_s:
        failures.append(f"first audio {latency:.2f} s > {limits.first_audio_s} s")
    cpu = max(_floats(report, "cpu_percent"), default=0.0)
    if cpu > limits.cpu_percent:
        failures.append(f"CPU {cpu:.1f} % > {limits.cpu_percent} %")
    rss = max(_floats(report, "rss_mb"), default=0.0)
    if rss > limits.rss_mb:
        failures.append(f"RSS {rss:.0f} MB > {limits.rss_mb} MB")
    return failures


def _soak_failures(report: dict[str, object], limits: GateLimits) -> list[str]:
    failures: list[str] = []
    problems = report["soak_problems"]
    if isinstance(problems, list) and problems:
        failures.append(f"soak problems: {problems}")
    if _number(report, "items_started") == 0:
        failures.append("soak: no item started")
    if _number(report, "decode_exit_code") != 0:
        failures.append(f"soak recording did not decode (exit {report['decode_exit_code']})")
    planned, streamed = _number(report, "soak_planned_s"), _number(report, "soak_streamed_s")
    if streamed < planned - limits.short_stream_s:
        failures.append(f"soak streamed {streamed:.1f} s of the planned {planned:.1f} s")
    silence = max(_floats(report, "silences_s"), default=0.0)
    if silence >= limits.max_silence_s:
        failures.append(f"silence {silence:.2f} s >= {limits.max_silence_s} s")
    allowed = _number(report, "max_span_s") + limits.freeze_margin_s
    starts = _floats(report, "start_times_s")
    gap = longest_gap_s([*starts, _number(report, "soak_end_s")]) if starts else 0.0
    if gap > allowed:
        failures.append(f"freeze: {gap:.1f} s without a start > {allowed} s")
    return failures


def gate_failures(report: dict[str, object], limits: GateLimits) -> list[str]:
    missing = [key for key in REQUIRED if key not in report]
    if missing:
        return [f"missing measurement: {key}" for key in missing]
    return _resource_failures(report, limits) + _soak_failures(report, limits)
