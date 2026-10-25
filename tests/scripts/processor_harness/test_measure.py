"""Each input kind gets its own analysis; a render that changed nothing reads as such."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from processor_harness import measure, signals
from processor_harness.measure import Rendered
from processor_harness.metrics import bands, loudness
from scipy import signal as sp

FS = 44100


def untouched(
    stim: signals.Stimulus,
    gauges: dict[str, tuple[np.ndarray, np.ndarray]] | None = None,
    decoded: np.ndarray | None = None,
    out: np.ndarray | None = None,
) -> Rendered:
    """A render whose output is its input (or ``out``, when given)."""
    return Rendered(
        inp=stim.samples,
        out=stim.samples if out is None else out,
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

    assert {k: row[k] for k in ("band_sum_min_db", "band_sum_max_db", "latency_ms")} == (
        pytest.approx({"band_sum_min_db": 0.0, "band_sum_max_db": 0.0, "latency_ms": 0.0}, abs=0.01)
    )


def test_impulse_reports_the_third_octave_response() -> None:
    # A solo band (one crossover band rendered alone) shows its shape here: a 1 kHz
    # fourth-order low-pass is flat at 125 Hz and far down at 4 kHz.
    stim = signals.impulse(FS, level_dbfs=-30.0, seconds=2.0, at_s=0.5)
    sos = sp.butter(4, 1000.0, fs=FS, output="sos")
    rendered = untouched(stim)
    low_passed = Rendered(
        inp=stim.samples,
        out=sp.sosfilt(sos, stim.samples, axis=0),
        fs=FS,
        segments=stim.segments,
        gauges={},
        decoded=None,
        ceiling_dbtp=-1.0,
        latency_s=0.0,
    )

    flat = measure.measure("impulse", rendered)
    row = measure.measure("impulse", low_passed)

    assert flat["response_25hz_db"] == pytest.approx(0.0, abs=0.01)
    assert flat["response_16000hz_db"] == pytest.approx(0.0, abs=0.01)
    assert row["response_125hz_db"] == pytest.approx(0.0, abs=0.1)
    assert row["response_4000hz_db"] < -40.0


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


def test_programme_reports_what_moved_against_the_input() -> None:
    # Output 3 dB louder than its input, with one sample pushed to -2 dBFS: peaks and
    # loudness are reported both as levels and as changes from the input, so it is clear
    # which of the two moved.
    stim = signals.noise_with_pauses(FS, burst_s=10.0, pause_s=0.5, bursts=1, level_dbfs=-24.0)
    out = stim.samples * 10 ** (3 / 20)
    out[FS, :] = 10 ** (-2 / 20)
    rendered = Rendered(
        inp=stim.samples,
        out=out,
        fs=FS,
        segments=stim.segments,
        gauges={},
        decoded=None,
        ceiling_dbtp=-1.0,
        latency_s=0.0,
    )

    row = measure.measure("programme", rendered)

    assert row["sample_peak_dbfs"] == pytest.approx(-2.0, abs=1e-6)
    assert row["loudness_change_lu"] == pytest.approx(3.0, abs=0.05)
    input_peak = loudness.true_peak(stim.samples, FS)
    assert row["true_peak_rise_db"] == pytest.approx(row["true_peak_dbtp"] - input_peak, abs=1e-9)


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


PRE_RING = ("pre_ring_energy_db", "pre_ring_reach_ms", "pre_ring_beyond_20ms_db")


def test_hits_and_the_click_report_pre_ringing() -> None:
    kick = measure.measure("kick", untouched(signals.kick(FS, -6.0, 1.5, 0.5)))
    snare = measure.measure("snare", untouched(signals.snare(FS, -6.0, 1.5, 0.5)))
    click = measure.measure("impulse", untouched(signals.impulse(FS, -30.0, 2.0, 0.5)))

    for row in (kick, snare, click):
        assert row["pre_ring_reach_ms"] == 0.0
        assert row["pre_ring_beyond_20ms_db"] == float("-inf")
    assert set(kick) == set(PRE_RING)


def test_only_transient_programme_reports_pre_ringing() -> None:
    stim = signals.noise_with_pauses(FS, burst_s=4.0, pause_s=0.5, bursts=1, level_dbfs=-18.0)

    def row(category: str) -> dict[str, float]:
        rendered = Rendered(
            inp=stim.samples,
            out=stim.samples,
            fs=FS,
            segments=stim.segments,
            gauges={},
            decoded=None,
            ceiling_dbtp=-1.0,
            latency_s=0.0,
            category=category,
        )
        return measure.measure("programme", rendered)

    assert set(PRE_RING) <= set(row("transient"))
    assert not set(PRE_RING) & set(row("music"))


def test_the_click_lines_up_its_pre_ringing_by_its_own_latency() -> None:
    # The impulse is where the chain's latency is measured, so its own pre-ring must use
    # that latency, not the one handed in (0 before any impulse has been measured).
    stim = signals.impulse(FS, level_dbfs=-30.0, seconds=2.0, at_s=0.5)
    out = stim.samples + 0.01 * np.roll(stim.samples, -round(0.030 * FS), axis=0)
    out = np.roll(out, round(0.050 * FS), axis=0)  # a 50 ms linear-phase chain

    row = measure.measure("impulse", untouched(stim, out=out))

    assert row["latency_ms"] == pytest.approx(50.0, abs=0.05)
    assert row["pre_ring_reach_ms"] == pytest.approx(30.0, abs=1.0)


def test_a_declared_acompressor_switches_the_fits_to_its_model() -> None:
    from processor_harness.metrics import acompressor
    from processor_harness.metrics.acompressor import Settings

    s = Settings.from_db(-24.0, 4.0, 9.03, 6.0, 20.0, 250.0)
    steps = signals.stepped_tone(FS, 1000.0, -40.0, 0.0, hold_s=1.0)
    bursts = signals.tone_bursts(FS, 1000.0, -30.0, 10.0, hold_s=1.0, cycles=2)

    def compressed(stim: signals.Stimulus) -> Rendered:
        return Rendered(
            inp=stim.samples,
            out=acompressor.process(stim.samples, FS, s),
            fs=FS,
            segments=stim.segments,
            gauges={},
            decoded=None,
            ceiling_dbtp=-1.0,
            latency_s=0.0,
            model=s,
        )

    static = measure.measure("stepped_tone", compressed(steps))
    times = measure.measure("tone_bursts", compressed(bursts))

    assert static["static_ratio"] == pytest.approx(4.0, rel=0.02)
    assert times["attack_ms"] == pytest.approx(20.0, rel=0.05)
    assert times["attack_tau_ms"] == pytest.approx(5.0, rel=0.05)
    assert times["release_tau_ms"] == pytest.approx(62.5, rel=0.05)


def test_programme_reports_where_each_logged_gain_spent_its_time() -> None:
    # The AGC-target step asks how close agc.liq's gain runs to its +-10 dB range: the spread of
    # every logged gauge over the render (5th, 50th and 95th percentile, extremes).
    stim = signals.noise_with_pauses(FS, burst_s=10.0, pause_s=0.5, bursts=1, level_dbfs=-18.0)
    times = np.arange(0.0, 10.0, 0.02)
    gain = np.linspace(-2.0, 8.0, len(times))
    rendered = Rendered(
        inp=stim.samples,
        out=stim.samples,
        fs=FS,
        segments=stim.segments,
        gauges={"agc_gain": (times, gain)},
        decoded=None,
        ceiling_dbtp=-1.0,
        latency_s=0.0,
    )

    row = measure.measure("programme", rendered)

    assert row["gauge.agc_gain.min"] == pytest.approx(-2.0)
    assert row["gauge.agc_gain.max"] == pytest.approx(8.0)
    assert row["gauge.agc_gain.p50"] == pytest.approx(3.0, abs=0.05)
    assert row["gauge.agc_gain.p05"] == pytest.approx(-1.5, abs=0.05)
    assert row["gauge.agc_gain.p95"] == pytest.approx(7.5, abs=0.05)


def test_pinned_time_counts_frames_within_1_db_of_a_declared_limit() -> None:
    # Lance, 2026-10-24: report how long the AGC's gain is pinned near its +-10 dB range.
    stim = signals.noise_with_pauses(FS, burst_s=10.0, pause_s=0.5, bursts=1, level_dbfs=-18.0)
    times = np.arange(0.0, 10.0, 0.02)  # 500 frames
    gain = np.zeros(len(times))
    gain[:50] = 9.5  # within 1 dB of +10
    gain[50:75] = -9.2  # within 1 dB of -10
    gain[75:100] = 8.9  # just outside
    rendered = Rendered(
        inp=stim.samples,
        out=stim.samples,
        fs=FS,
        segments=stim.segments,
        gauges={"agc_gain": (times, gain)},
        decoded=None,
        ceiling_dbtp=-1.0,
        latency_s=0.0,
        gauge_limits={"agc_gain": (-10.0, 10.0)},
    )

    row = measure.measure("programme", rendered)

    assert row["gauge.agc_gain.pinned_pct"] == pytest.approx(15.0)


def test_programme_reports_its_loudness_range_change() -> None:
    # Squeezing a song's dynamics is a cost for a music stream (R7): LRA out minus LRA in.
    loud = signals.noise_with_pauses(FS, burst_s=10.0, pause_s=0.1, bursts=1, level_dbfs=-14.0)
    soft = signals.noise_with_pauses(FS, burst_s=10.0, pause_s=0.1, bursts=1, level_dbfs=-24.0)
    inp = np.concatenate([loud.samples, soft.samples])
    out = np.concatenate([loud.samples, soft.samples * 10 ** (5 / 20)])  # range squeezed by 5 dB
    rendered = Rendered(
        inp=inp,
        out=out,
        fs=FS,
        segments=loud.segments,
        gauges={},
        decoded=None,
        ceiling_dbtp=-1.0,
        latency_s=0.0,
    )

    row = measure.measure("programme", rendered)

    assert row["lra_change_lu"] == pytest.approx(-5.0, abs=0.7)


def test_gain_travel_and_one_second_swing_show_how_much_a_gauge_moves() -> None:
    # The AGC timing step: a level-correcting AGC barely moves within a song; one that follows
    # the music (pumping) travels far and swings within seconds.
    stim = signals.noise_with_pauses(FS, burst_s=60.0, pause_s=0.1, bursts=1, level_dbfs=-18.0)
    times = np.arange(0.0, 60.0, 0.02)  # one minute of frames
    gain = 3.0 * np.sin(2 * np.pi * 0.5 * times)  # +-3 dB at 0.5 Hz: pumping
    rendered = Rendered(
        inp=stim.samples,
        out=stim.samples,
        fs=FS,
        segments=stim.segments,
        gauges={"agc_gain": (times, gain)},
        decoded=None,
        ceiling_dbtp=-1.0,
        latency_s=0.0,
    )

    row = measure.measure("programme", rendered)

    # 30 cycles of 4 x 3 dB each = 360 dB of travel in the minute.
    assert row["gauge.agc_gain.travel_db_per_min"] == pytest.approx(360.0, rel=0.01)
    # Over any 1 s (half a period) the gain swings by up to 6 dB.
    assert row["gauge.agc_gain.swing_1s_p95_db"] == pytest.approx(5.9, abs=0.2)


def test_gauge_rows_broken_by_an_interleaved_log_line_are_skipped(tmp_path: Path) -> None:
    # Liquidsoap's log shares stdout with the gauge lines and can land in the middle of one.
    csv = tmp_path / "g.csv"
    csv.write_text(
        "time_s,name,value\n0.0,agc_gain,1.0\n"
        "0.02,agc_gain,0.02026/10/24 22:34:45 [video.converter:3] Using video converter\n"
        "0.02026/10/24 22:34:45 [video.converter:3] Using preferred video converter: ffmpeg.\n"
        "0.04,agc_gain,2.0\n",
        encoding="utf-8",
    )

    times, values = measure.read_gauges(csv)["agc_gain"]

    np.testing.assert_array_equal(times, [0.0, 0.04])
    np.testing.assert_array_equal(values, [1.0, 2.0])


def test_programme_through_a_band_preset_reports_each_band() -> None:
    # Band 3 of 5 turned down 3 dB: its gain reduction, its levels, and the balance measures.
    edges = (100.0, 520.0, 1800.0, 6000.0)
    stim = signals.noise_with_pauses(FS, 8.0, 0.0, 1, -20.0)
    part = sp.oaconvolve(stim.samples, bands.kernels(edges, FS)[2][:, np.newaxis], "same", axes=0)
    out = stim.samples - part * (1 - 10 ** (-3 / 20))
    rendered = Rendered(**{**untouched(stim).__dict__, "out": out, "band_edges": edges})

    row = measure.measure(measure.PROGRAMME, rendered)

    assert row["band.3.gr_mean_db"] == pytest.approx(3.0, abs=0.05)
    assert row["band.3.out_db"] == pytest.approx(row["band.3.in_db"] - 3.0, abs=0.05)
    assert abs(row["band.1.gr_mean_db"]) < 0.05
    assert row["balance_change_db"] > 1.0
    assert row["balance_drift_db"] < 0.1
    assert "band.5.gr_p95_db" in row
    assert row["octave.1000hz.out_db"] < row["octave.1000hz.in_db"]
    assert row["octave_drift_db"] < 0.1 and row["octave_change_db"] > 0.5


def test_programme_without_band_edges_has_no_band_rows() -> None:
    stim = signals.noise_with_pauses(FS, 4.0, 0.0, 1, -20.0)

    row = measure.measure(measure.PROGRAMME, untouched(stim))

    assert not [k for k in row if k.startswith(("band.", "balance_"))]
