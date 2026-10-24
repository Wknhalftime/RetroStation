"""Each input kind gets its own analysis; a render that changed nothing reads as such."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from processor_harness import measure, signals
from processor_harness.measure import Rendered

FS = 44100


def untouched(
    stim: signals.Stimulus,
    gauges: dict[str, tuple[np.ndarray, np.ndarray]] | None = None,
    decoded: np.ndarray | None = None,
) -> Rendered:
    """A render whose output is its input."""
    return Rendered(
        inp=stim.samples,
        out=stim.samples,
        fs=FS,
        segments=stim.segments,
        gauges=gauges or {},
        decoded=decoded,
        ceiling_dbtp=-1.0,
        latency_s=0.0,
    )


def test_impulse_reports_band_sum_and_latency() -> None:
    stim = signals.impulse(FS, level_dbfs=-30.0, seconds=2.0, at_s=0.5)

    row = measure.measure("impulse", untouched(stim))

    assert row == pytest.approx(
        {"band_sum_min_db": 0.0, "band_sum_max_db": 0.0, "latency_ms": 0.0}, abs=0.01
    )


def test_sweep_reports_response_deviation() -> None:
    stim = signals.log_sweep(FS, 20.0, 20000.0, -30.0, 5.0)

    assert measure.measure("log_sweep", untouched(stim)) == pytest.approx(
        {"response_deviation_db": 0.0}, abs=0.01
    )


def test_stepped_tone_reports_the_fitted_static_curve() -> None:
    stim = signals.stepped_tone(FS, 1000.0, -40.0, 0.0, hold_s=0.5)

    row = measure.measure("stepped_tone", untouched(stim))

    assert set(row) == {
        "static_threshold_db",
        "static_ratio",
        "static_knee_db",
        "static_makeup_db",
        "static_fit_error_db",
    }
    assert row["static_ratio"] == pytest.approx(1.0, abs=0.01)
    assert row["static_makeup_db"] == pytest.approx(0.0, abs=0.05)


def test_tone_bursts_without_gain_change_have_no_time_constants() -> None:
    stim = signals.tone_bursts(FS, 1000.0, -30.0, 10.0, hold_s=1.0, cycles=2)

    row = measure.measure("tone_bursts", untouched(stim))

    assert np.isnan(row["attack_tau_ms"]) and np.isnan(row["release_tau_ms"])


def test_tone_and_twin_tones_report_distortion() -> None:
    assert measure.measure("tone", untouched(signals.tone(FS, 1000.0, -6.0, 2.0)))["thd_pct"] < 0.1
    smpte = signals.smpte_twin_tone(FS, -6.0, 2.0)
    assert measure.measure("smpte_imd", untouched(smpte))["imd_smpte_pct"] < 0.1
    ccif = signals.ccif_twin_tone(FS, -6.0, 2.0)
    assert measure.measure("ccif_imd", untouched(ccif))["imd_ccif_pct"] < 0.1


def test_pauses_report_each_gauges_rise_in_silence() -> None:
    stim = signals.noise_with_pauses(FS, burst_s=1.0, pause_s=2.0, bursts=2, level_dbfs=-20.0)
    times = np.arange(0.0, 6.0, 0.02)
    climbing = np.where((times >= 4.0) & (times < 6.0), times - 4.0, 0.0)
    gauges = {"agc_gain": (times, climbing), "flat": (times, np.zeros_like(times))}

    row = measure.measure("noise_with_pauses", untouched(stim, gauges=gauges))

    assert row["pause_rise.agc_gain_db"] == pytest.approx(1.98, abs=0.03)
    assert row["pause_rise.flat_db"] == 0.0


def test_programme_reports_loudness_peaks_and_the_codec_round_trip() -> None:
    stim = signals.noise_with_pauses(FS, burst_s=10.0, pause_s=0.5, bursts=1, level_dbfs=-18.0)
    decoded = stim.samples * 1.2  # the codec rebuilt peaks 1.6 dB higher

    row = measure.measure("programme", untouched(stim, decoded=decoded))

    for key in (
        "lufs_i",
        "lra_lu",
        "true_peak_dbtp",
        "plr_lu",
        "psr_p10_lu",
        "psr_p50_lu",
        "psr_p90_lu",
        "crest_db",
        "sample_overs",
        "true_overs",
        "codec_true_peak_dbtp",
        "codec_true_overs",
    ):
        assert key in row, key
    assert row["codec_true_peak_dbtp"] - row["true_peak_dbtp"] == pytest.approx(1.58, abs=0.02)


def test_unknown_kinds_are_refused() -> None:
    with pytest.raises(KeyError, match="chirp"):
        measure.measure("chirp", untouched(signals.tone(FS, 1000.0, -6.0, 1.0)))


def test_gauges_csv_reads_into_one_trace_per_name(tmp_path: Path) -> None:
    csv = tmp_path / "g.csv"
    csv.write_text("time_s,name,value\n0.0,a,1.0\n0.02,a,2.0\n0.0,b,-inf\n", encoding="utf-8")

    gauges = measure.read_gauges(csv)

    np.testing.assert_array_equal(gauges["a"][0], [0.0, 0.02])
    np.testing.assert_array_equal(gauges["a"][1], [1.0, 2.0])
    assert gauges["b"][1][0] == float("-inf")
