"""Fits against the brief's math items: static curve (2), time constants (1, 3), AGC gating (4)."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest
from processor_harness import signals
from processor_harness.metrics import acompressor, fits
from processor_harness.metrics.acompressor import Settings
from processor_harness.signals import Segment

FS = 44100


def soft_knee(x: float, t: float, r: float, w: float, makeup: float) -> float:
    """compress.ml's static curve (brief math item 2), plus makeup gain."""
    if x <= t - w / 2:
        y = x
    elif x >= t + w / 2:
        y = t + (x - t) / r
    else:
        y = x + (1 / r - 1) * (x - t + w / 2) ** 2 / (2 * w)
    return y + makeup


def test_static_levels_read_each_step_after_it_settles() -> None:
    stim = signals.stepped_tone(FS, freq=1000.0, start_dbfs=-40.0, stop_dbfs=0.0, hold_s=0.5)
    out = signals._tone_at_levels(
        FS, 1000.0, tuple(Segment(s.start_s, s.stop_s, s.level_dbfs - 3.0) for s in stim.segments)
    ).samples

    points = fits.static_levels(stim.segments, out, FS)

    assert [p[0] for p in points] == [s.level_dbfs for s in stim.segments]
    np.testing.assert_allclose(
        [p[1] for p in points], [s.level_dbfs - 3.0 for s in stim.segments], atol=0.05
    )


def test_soft_knee_fit_recovers_threshold_ratio_knee_and_makeup() -> None:
    levels = np.arange(-40.0, 0.5, 2.0)
    points = [(x, soft_knee(x, -20.0, 4.0, 6.0, 2.0)) for x in levels]

    fit = fits.fit_static_curve(points)

    assert fit.threshold_db == pytest.approx(-20.0, abs=0.1)
    assert fit.ratio == pytest.approx(4.0, rel=0.02)
    assert fit.knee_db == pytest.approx(6.0, abs=0.3)
    assert fit.makeup_db == pytest.approx(2.0, abs=0.05)
    assert fit.rms_error_db < 0.01


def one_pole_gain_trace(
    segments: tuple[Segment, ...], fs: int, gr_db: float, attack_s: float, release_s: float
) -> np.ndarray:
    """dB gain a compressor with these time constants applies: gr_db of reduction on every
    loud segment, smoothed in the dB domain with separate attack and release coefficients."""
    n = round(segments[-1].stop_s * fs)
    target = np.zeros(n)
    base = segments[0].level_dbfs
    for seg in segments:
        if seg.level_dbfs > base:
            target[round(seg.start_s * fs) : round(seg.stop_s * fs)] = -gr_db
    ca, cr = 1 - np.exp(-1 / (attack_s * fs)), 1 - np.exp(-1 / (release_s * fs))
    g, out = 0.0, np.empty(n)
    for i, t in enumerate(target):
        g += (ca if t < g else cr) * (t - g)
        out[i] = g
    return out


def test_time_constants_are_recovered_from_a_compressed_burst() -> None:
    stim = signals.tone_bursts(FS, 1000.0, base_dbfs=-30.0, step_db=10.0, hold_s=1.0, cycles=2)
    gain_db = one_pole_gain_trace(stim.segments, FS, gr_db=6.0, attack_s=0.010, release_s=0.200)
    out = stim.samples * (10 ** (gain_db / 20))[:, np.newaxis]

    tc = fits.time_constants(stim.segments, stim.samples, out, FS, latency_s=0.0)

    assert tc.attack_s == pytest.approx(0.010, rel=0.10)
    assert tc.release_s == pytest.approx(0.200, rel=0.10)


def test_time_constants_line_up_a_delayed_output() -> None:
    stim = signals.tone_bursts(FS, 1000.0, base_dbfs=-30.0, step_db=10.0, hold_s=1.0, cycles=2)
    gain_db = one_pole_gain_trace(stim.segments, FS, gr_db=6.0, attack_s=0.010, release_s=0.200)
    out = np.roll(stim.samples * (10 ** (gain_db / 20))[:, np.newaxis], 220, axis=0)

    tc = fits.time_constants(stim.segments, stim.samples, out, FS, latency_s=220 / FS)

    assert tc.attack_s == pytest.approx(0.010, rel=0.10)
    assert tc.release_s == pytest.approx(0.200, rel=0.10)


def test_pause_rise_is_the_largest_gain_climb_inside_any_pause() -> None:
    pauses = (Segment(2.0, 4.0, signals.SILENT), Segment(6.0, 8.0, signals.SILENT))
    times = np.arange(0.0, 9.0, 0.02)
    gain = np.where((times >= 6.0) & (times < 8.0), (times - 6.0) * 0.5, 0.0)  # +1 dB by 8 s

    assert fits.pause_rise(times, gain, pauses) == pytest.approx(1.0, abs=0.02)


def test_pause_rise_ignores_falling_gain() -> None:
    pauses = (Segment(1.0, 3.0, signals.SILENT),)
    times = np.arange(0.0, 4.0, 0.02)

    assert fits.pause_rise(times, -times, pauses) == pytest.approx(0.0, abs=1e-9)


TRUE = Settings.from_db(
    threshold_db=-24.0, ratio=4.0, knee_db=9.03, makeup_db=6.0, attack_ms=20.0, release_ms=250.0
)


def test_acompressor_static_fit_recovers_the_settings_through_the_model() -> None:
    # The detector rides a rectified sine between its mean and its peak, so the static curve
    # is fitted by running the 3a model, not with a closed-form curve.
    stim = signals.stepped_tone(FS, 1000.0, -40.0, 0.0, hold_s=1.0)
    out = acompressor.process(stim.samples, FS, TRUE)

    fit = fits.fit_acompressor_static(stim.segments, stim.samples, out, FS, TRUE, latency_s=0.0)

    assert fit.threshold_db == pytest.approx(-24.0, abs=0.1)
    assert fit.ratio == pytest.approx(4.0, rel=0.02)
    assert fit.knee_db == pytest.approx(9.03, abs=0.3)
    assert fit.makeup_db == pytest.approx(6.0, abs=0.05)
    assert fit.rms_error_db < 0.05


def test_acompressor_time_fit_recovers_attack_and_release_through_the_model() -> None:
    stim = signals.tone_bursts(FS, 1000.0, base_dbfs=-30.0, step_db=10.0, hold_s=1.0, cycles=2)
    out = acompressor.process(stim.samples, FS, TRUE)

    start = replace(TRUE, attack_ms=40.0, release_ms=500.0)  # the fit must find them itself

    times = fits.fit_acompressor_times(stim.segments, stim.samples, out, FS, start, latency_s=0.0)

    assert times.attack_ms == pytest.approx(20.0, rel=0.05)
    assert times.release_ms == pytest.approx(250.0, rel=0.05)
    assert times.rms_error_db < 0.05


def test_acompressor_time_fit_reports_nothing_when_the_gain_barely_moves() -> None:
    # Threshold above the loud burst: the gain hardly moves, so no time constant is measurable,
    # even though noise-free data would still pin the model's values.
    idle = Settings.from_db(-18.0, 4.0, 0.0, 0.0, 20.0, 250.0)
    stim = signals.tone_bursts(FS, 1000.0, base_dbfs=-30.0, step_db=10.0, hold_s=1.0, cycles=2)
    out = acompressor.process(stim.samples, FS, idle)

    times = fits.fit_acompressor_times(stim.segments, stim.samples, out, FS, idle, latency_s=0.0)

    assert math.isnan(times.attack_ms) and math.isnan(times.release_ms)


def test_acompressor_time_fit_lines_up_a_delayed_output() -> None:
    stim = signals.tone_bursts(FS, 1000.0, base_dbfs=-30.0, step_db=10.0, hold_s=1.0, cycles=2)
    out = np.roll(acompressor.process(stim.samples, FS, TRUE), round(0.020 * FS), axis=0)
    start = replace(TRUE, attack_ms=10.0, release_ms=125.0)

    times = fits.fit_acompressor_times(stim.segments, stim.samples, out, FS, start, latency_s=0.020)

    assert times.attack_ms == pytest.approx(20.0, rel=0.05)
    assert times.release_ms == pytest.approx(250.0, rel=0.05)


def test_acompressor_static_fit_leaves_out_steps_the_render_never_reached() -> None:
    # A chain with latency renders as long as its input, so its last 20 ms (here 60 ms, as
    # Liquidsoap cut the multiband's render) never arrive: that step must not count as quiet.
    stim = signals.stepped_tone(FS, 1000.0, -40.0, 0.0, hold_s=0.6)
    s = Settings.from_db(-24.0, 3.0, 9.03, 0.0, 20.0, 250.0)
    model = acompressor.process(stim.samples, FS, s)
    late = np.concatenate([np.zeros((882, 2)), model])[: len(model) - round(0.06 * FS)]

    fit = fits.fit_acompressor_static(stim.segments, stim.samples, late, FS, s, 0.02)

    assert fit.threshold_db == pytest.approx(-24.0, abs=0.05)
    assert fit.ratio == pytest.approx(3.0, rel=0.01)
