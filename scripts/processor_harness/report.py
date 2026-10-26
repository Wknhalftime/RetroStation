"""Results on disk: ``<preset>-v<version>.json`` with every input's metrics, ``summary.csv`` and
``summary.md`` with one row per preset, and ``comparison.csv`` with one row per input and
metric and a column per preset. Every row names the renderer and the standalone FFmpeg, since
a value tuned through one FFmpeg can differ inside the other.

Summary rules: worst case for peaks, overs counts, deviations and latency (overs summed); the
median otherwise, with ``lufs_i_spread`` (highest minus lowest) beside the loudness.
"""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median

_IDENTITY_COLUMNS = ("preset", "version", "backend", "renderer", "standalone_ffmpeg")
_SUM = {"sample_overs", "true_overs", "codec_true_overs", "recovery_missed"}
_MAX = {
    "true_peak_dbtp",
    "true_peak_rise_db",
    "sample_peak_dbfs",
    "codec_true_peak_dbtp",
    "band_sum_max_db",
    "latency_ms",
    "response_deviation_db",
    "static_fit_error_db",
    "thd_pct",
    "imd_smpte_pct",
    "imd_ccif_pct",
    "pre_ring_energy_db",
    "pre_ring_reach_ms",
    "pre_ring_beyond_20ms_db",
    "recovery_s_max",
    "item_lufs_spread",
}
_MIN = {"band_sum_min_db"}


@dataclass(frozen=True)
class PresetResult:
    preset_id: str
    version: int
    backend: str
    renderer: str
    standalone_ffmpeg: str
    metrics: Mapping[str, Mapping[str, float]]


def _rule(metric: str) -> Callable[[list[float]], float]:
    if metric in _SUM:
        return lambda v: float(sum(v))
    if (
        metric in _MAX
        or metric.startswith("pause_rise.")
        or metric.endswith((".max", ".pinned_pct"))
    ):
        return lambda v: float(max(v))
    if metric in _MIN or (metric.startswith("gauge.") and metric.endswith(".min")):
        return lambda v: float(min(v))
    return lambda v: float(median(v))


def summary_row(result: PresetResult) -> dict[str, str | int | float]:
    """One row for the preset: identity columns, then each metric aggregated by its rule."""
    row: dict[str, str | int | float] = {
        "preset": result.preset_id,
        "version": result.version,
        "backend": result.backend,
        "renderer": result.renderer,
        "standalone_ffmpeg": result.standalone_ffmpeg,
    }
    names = sorted({m for metrics in result.metrics.values() for m in metrics})
    for name in names:
        values = [float(m[name]) for m in result.metrics.values() if name in m]
        finite = [v for v in values if not math.isnan(v)]
        row[name] = _rule(name)(finite) if finite else math.nan
        if name == "lufs_i" and finite:
            row["lufs_i_spread"] = max(finite) - min(finite)
    return row


def _json_value(value: float) -> float | None:
    return None if math.isnan(value) or math.isinf(value) else value


def _write_json(result: PresetResult, directory: Path) -> None:
    detail = {
        "preset": result.preset_id,
        "version": result.version,
        "backend": result.backend,
        "renderer": result.renderer,
        "standalone_ffmpeg": result.standalone_ffmpeg,
        "metrics": {
            input_id: {k: _json_value(float(v)) for k, v in metrics.items()}
            for input_id, metrics in result.metrics.items()
        },
    }
    path = directory / f"{result.preset_id}-v{result.version}.json"
    path.write_text(json.dumps(detail, indent=2) + "\n", encoding="utf-8")


def _write_summary(rows: list[dict[str, str | int | float]], directory: Path) -> None:
    columns = list(_IDENTITY_COLUMNS) + sorted(
        {k for row in rows for k in row} - set(_IDENTITY_COLUMNS)
    )
    with (directory / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, columns)
        writer.writeheader()
        writer.writerows(rows)
    metrics = [c for c in columns if c not in ("renderer", "standalone_ffmpeg")]
    lines = ["| " + " | ".join(metrics) + " |", "|" + "---|" * len(metrics)]
    for row in rows:
        cells = [_cell(row.get(c, "")) for c in metrics]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines += [f"- {row['preset']}: {row['renderer']}; {row['standalone_ffmpeg']}" for row in rows]
    (directory / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cell(value: str | int | float) -> str:
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def _write_comparison(results: Sequence[PresetResult], directory: Path) -> None:
    keys = sorted({(i, m) for r in results for i, ms in r.metrics.items() for m in ms})
    with (directory / "comparison.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["input", "metric", *(r.preset_id for r in results)])
        for input_id, metric in keys:
            values = [r.metrics.get(input_id, {}).get(metric, "") for r in results]
            writer.writerow([input_id, metric, *values])


def write_reports(results: Sequence[PresetResult], directory: Path) -> None:
    """All three report files for one run."""
    directory.mkdir(parents=True, exist_ok=True)
    for result in results:
        _write_json(result, directory)
    _write_summary([summary_row(r) for r in results], directory)
    _write_comparison(results, directory)
