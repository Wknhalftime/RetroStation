"""Results: JSON per preset, one summary row per preset, one long comparison table."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import pytest
from processor_harness.report import PresetResult, summary_row, write_reports


def result(preset_id: str, lufs: tuple[float, float], overs: tuple[int, int]) -> PresetResult:
    return PresetResult(
        preset_id=preset_id,
        version=3,
        backend="liquidsoap",
        renderer="Liquidsoap 2.4.5 (libavcodec 61.19.100, libavformat 61.7.100)",
        standalone_ffmpeg="ffmpeg version 8.0.1 (libavfilter 11.4.100)",
        metrics={
            "song-a": {"lufs_i": lufs[0], "true_overs": overs[0], "true_peak_dbtp": -1.4},
            "song-b": {"lufs_i": lufs[1], "true_overs": overs[1], "true_peak_dbtp": -0.9},
            "impulse": {"latency_ms": 5.0, "band_sum_min_db": -0.02},
            "bursts": {"attack_tau_ms": float("nan")},
        },
    )


def test_summary_row_aggregates_each_metric_by_its_rule() -> None:
    row = summary_row(result("new", (-16.0, -17.0), (2, 3)))

    assert row["preset"] == "new" and row["version"] == 3
    assert row["renderer"].startswith("Liquidsoap 2.4.5")
    assert row["standalone_ffmpeg"].startswith("ffmpeg version 8.0.1")
    assert row["lufs_i"] == pytest.approx(-16.5)  # median
    assert row["lufs_i_spread"] == pytest.approx(1.0)
    assert row["true_overs"] == 5  # summed
    assert row["true_peak_dbtp"] == pytest.approx(-0.9)  # worst
    assert row["band_sum_min_db"] == pytest.approx(-0.02)
    assert math.isnan(row["attack_tau_ms"])


def test_reports_write_json_summary_and_comparison(tmp_path: Path) -> None:
    results = [result("bypass", (-14.0, -20.0), (0, 0)), result("new", (-16.0, -16.2), (1, 0))]

    write_reports(results, tmp_path)

    detail = json.loads((tmp_path / "new-v3.json").read_text(encoding="utf-8"))
    assert detail["metrics"]["song-a"]["lufs_i"] == -16.0
    assert detail["renderer"].startswith("Liquidsoap")
    assert detail["metrics"]["bursts"]["attack_tau_ms"] is None  # nan -> null

    with (tmp_path / "summary.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["preset"] for r in rows] == ["bypass", "new"]
    assert rows[1]["standalone_ffmpeg"].startswith("ffmpeg version 8.0.1")

    with (tmp_path / "comparison.csv").open(encoding="utf-8") as f:
        long = list(csv.DictReader(f))
    lufs_a = next(r for r in long if r["input"] == "song-a" and r["metric"] == "lufs_i")
    assert (lufs_a["bypass"], lufs_a["new"]) == ("-14.0", "-16.0")

    assert "| preset |" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_band_levels_also_report_their_spread_between_songs() -> None:
    # A music stream wants each band's level consistent from song to song (Lance, 2026-10-25).
    r = PresetResult(
        preset_id="bands",
        version=1,
        backend="liquidsoap",
        renderer="Liquidsoap 2.4.5",
        standalone_ffmpeg="ffmpeg version 8.0.1",
        metrics={
            "song-a": {"band.1.out_db": -30.0, "band.1.in_db": -28.0},
            "song-b": {"band.1.out_db": -27.5, "band.1.in_db": -24.0},
            "song-c": {"band.1.out_db": -29.0, "band.1.in_db": -26.0},
        },
    )

    row = summary_row(r)

    assert row["band.1.out_db"] == pytest.approx(-29.0)  # median
    assert row["band.1.out_spread_db"] == pytest.approx(2.5)
    assert row["band.1.in_spread_db"] == pytest.approx(4.0)
