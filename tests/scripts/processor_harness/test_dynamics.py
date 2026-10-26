"""Crest factor, PLR / PSR and counts of samples over the ceiling."""

from __future__ import annotations

import numpy as np
import pytest
from processor_harness.metrics import dynamics, loudness

FS = 44100


def sine(level_dbfs: float, seconds: float, freq: float = 1000.0) -> np.ndarray:
    t = np.arange(int(seconds * FS)) / FS
    mono = 10 ** (level_dbfs / 20) * np.sin(2 * np.pi * freq * t)
    return np.column_stack([mono, mono])


def quarter_rate_sine(amplitude: float, seconds: float) -> np.ndarray:
    """Every sample 3.01 dB under the true peak."""
    n = np.arange(int(seconds * FS))
    mono = amplitude * np.sin(np.pi / 2 * n + np.pi / 4)
    return np.column_stack([mono, mono])


def test_crest_factor_of_a_sine_is_3_db() -> None:
    assert dynamics.crest_factor(sine(-10.0, 1.0)) == pytest.approx(3.01, abs=0.01)


def test_plr_of_a_steady_sine_is_its_true_peak_minus_its_loudness() -> None:
    # A 1 kHz stereo sine at -23 dBFS peak reads -23 LUFS, so its PLR is about 0 LU.
    assert dynamics.plr(sine(-23.0, 10.0), FS) == pytest.approx(0.0, abs=0.2)


def test_psr_is_one_value_per_short_term_window() -> None:
    x = sine(-23.0, 10.0)

    values = dynamics.psr(x, FS)

    assert len(values) == len(loudness.short_term(x, FS))
    np.testing.assert_allclose(values, 0.0, atol=0.2)


def test_psr_skips_windows_below_the_absolute_gate() -> None:
    x = np.concatenate([sine(-23.0, 5.0), np.zeros((5 * FS, 2))])

    values = dynamics.psr(x, FS)

    assert np.all(np.isfinite(values))
    assert len(values) < len(loudness.short_term(x, FS))


def test_sample_overs_count_samples_above_the_ceiling() -> None:
    x = np.zeros((100, 2))
    x[10, 0] = 0.95  # -0.45 dBFS: over a -1 dBFS ceiling
    x[20, 1] = -0.95
    x[30, 0] = 0.85  # -1.4 dBFS: under it

    assert dynamics.sample_overs(x, ceiling_dbfs=-1.0) == 2


def test_true_overs_catch_peaks_between_samples() -> None:
    x = quarter_rate_sine(1.0, 1.0)  # samples at -3.01 dBFS, true peak 0 dBTP

    assert dynamics.sample_overs(x, ceiling_dbfs=-1.0) == 0
    assert dynamics.true_overs(x, FS, ceiling_dbtp=-1.0) > 0
