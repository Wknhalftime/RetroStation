"""BS.1770 loudness, EBU loudness range and true peak, checked against the published test
cases (EBU Tech 3341 and 3342) rather than against the implementation's own maths."""

from __future__ import annotations

import numpy as np
import pytest
from processor_harness.metrics import loudness

FS = 44100


def sine(level_dbfs: float, seconds: float, freq: float = 1000.0, fs: int = FS) -> np.ndarray:
    """Stereo sine, same on both channels; level is the peak in dBFS, as the EBU cases use."""
    t = np.arange(int(seconds * fs)) / fs
    mono = 10 ** (level_dbfs / 20) * np.sin(2 * np.pi * freq * t)
    return np.column_stack([mono, mono])


def test_k_weighting_at_48k_matches_the_bs1770_coefficient_table() -> None:
    sos = loudness.k_weighting(48000)

    shelf_b = [1.53512485958697, -2.69169618940638, 1.19839281085285]
    shelf_a = [1.0, -1.69065929318241, 0.73248077421585]
    hp_b = [1.0, -2.0, 1.0]
    hp_a = [1.0, -1.99004745483398, 0.99007225036621]
    np.testing.assert_allclose(sos[0], shelf_b + shelf_a, atol=1e-6)
    np.testing.assert_allclose(sos[1], hp_b + hp_a, atol=1e-6)


def test_stereo_1khz_sine_at_minus_23_dbfs_reads_minus_23_lufs() -> None:
    # EBU Tech 3341 case 1.
    assert loudness.integrated(sine(-23.0, 20.0), FS) == pytest.approx(-23.0, abs=0.1)


def test_relative_gate_drops_the_quiet_minus_36_sections() -> None:
    # EBU Tech 3341 case 3: -36 / -23 / -36 dBFS for 10 / 60 / 10 s reads -23.
    x = np.concatenate([sine(-36.0, 10.0), sine(-23.0, 60.0), sine(-36.0, 10.0)])
    assert loudness.integrated(x, FS) == pytest.approx(-23.0, abs=0.1)


def test_gated_average_of_unequal_sections() -> None:
    # EBU Tech 3341 case 5: -26 / -20 / -26 dBFS for 20.1 / 20 / 20.1 s reads -23.
    x = np.concatenate([sine(-26.0, 20.1), sine(-20.0, 20.0), sine(-26.0, 20.1)])
    assert loudness.integrated(x, FS) == pytest.approx(-23.0, abs=0.1)


def test_silence_has_no_integrated_loudness() -> None:
    assert loudness.integrated(np.zeros((FS * 5, 2)), FS) == float("-inf")


def test_loudness_range_of_a_10_db_step_is_10_lu() -> None:
    # EBU Tech 3342 case 1: 20 s at -20 dBFS then 20 s at -30 dBFS.
    x = np.concatenate([sine(-20.0, 20.0), sine(-30.0, 20.0)])
    assert loudness.loudness_range(x, FS) == pytest.approx(10.0, abs=1.0)


def test_loudness_range_of_a_5_db_step_is_5_lu() -> None:
    # EBU Tech 3342 case 2: 20 s at -20 dBFS then 20 s at -15 dBFS.
    x = np.concatenate([sine(-20.0, 20.0), sine(-15.0, 20.0)])
    assert loudness.loudness_range(x, FS) == pytest.approx(5.0, abs=1.0)


def test_short_term_loudness_steps_every_100_ms_over_3_s_windows() -> None:
    values = loudness.short_term(sine(-23.0, 10.0), FS)

    assert len(values) == 71  # (10 s - 3 s) / 0.1 s + 1
    np.testing.assert_allclose(values, -23.0, atol=0.1)


def test_true_peak_finds_the_peak_between_samples() -> None:
    # A quarter-rate sine sampled 45 degrees off its peaks: every sample sits 3.01 dB below
    # the true peak (EBU Tech 3341 true-peak cases use the same construction).
    n = np.arange(FS * 2)
    mono = np.sin(np.pi / 2 * n + np.pi / 4)
    x = np.column_stack([mono, mono])

    assert 20 * np.log10(np.max(np.abs(x))) == pytest.approx(-3.01, abs=0.01)
    assert loudness.true_peak(x, FS) == pytest.approx(0.0, abs=0.2)


def test_true_peak_of_silence_is_minus_infinity() -> None:
    assert loudness.true_peak(np.zeros((FS, 2)), FS) == float("-inf")
