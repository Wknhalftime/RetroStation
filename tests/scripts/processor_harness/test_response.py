"""Band-sum flatness and latency from the impulse, frequency response from the sweep."""

from __future__ import annotations

import numpy as np
import pytest
from processor_harness import signals
from processor_harness.metrics import response
from scipy import signal as sp

FS = 44100


def test_untouched_impulse_sums_flat_with_no_latency() -> None:
    s = signals.impulse(FS, level_dbfs=-30.0, seconds=2.0, at_s=0.5).samples

    low, high = response.band_sum_range(s, s, FS)

    assert low == pytest.approx(0.0, abs=0.01) and high == pytest.approx(0.0, abs=0.01)
    assert response.latency(s, s, FS) == 0.0


def test_latency_is_the_delay_of_the_output() -> None:
    s = signals.impulse(FS, level_dbfs=-30.0, seconds=2.0, at_s=0.5).samples
    delayed = np.roll(s, 221, axis=0)

    assert response.latency(s, delayed, FS) == pytest.approx(221 / FS)


def test_band_sum_range_is_relative_to_the_level_at_1khz() -> None:
    s = signals.impulse(FS, level_dbfs=-30.0, seconds=2.0, at_s=0.5).samples
    quieter = s * 10 ** (-3 / 20)

    low, high = response.band_sum_range(s, quieter, FS)

    assert low == pytest.approx(0.0, abs=0.01) and high == pytest.approx(0.0, abs=0.01)


def test_a_notch_shows_up_in_the_band_sum() -> None:
    s = signals.impulse(FS, level_dbfs=-30.0, seconds=2.0, at_s=0.5).samples
    # Narrow (Q 10), so its skirt leaves the 1 kHz reference alone.
    b, a = sp.iirnotch(400.0, 10.0, FS)
    notched = sp.lfilter(b, a, s, axis=0)

    low, high = response.band_sum_range(s, notched, FS)

    assert low < -20.0
    assert high == pytest.approx(0.0, abs=0.1)


def test_flat_chain_has_a_flat_sweep_response() -> None:
    s = signals.log_sweep(FS, f1=20.0, f2=20000.0, level_dbfs=-30.0, seconds=10.0).samples

    assert response.sweep_deviation(s, s, FS) == pytest.approx(0.0, abs=0.01)


def test_sweep_response_finds_a_6_db_high_shelf() -> None:
    s = signals.log_sweep(FS, f1=20.0, f2=20000.0, level_dbfs=-30.0, seconds=10.0).samples
    # A first-order shelf (zero at 5 kHz, pole at 10 kHz): +6 dB at the top, +0.1 dB at 1 kHz,
    # about +5 dB in the 16 kHz band.
    b, a = sp.bilinear([1.0 / (2 * np.pi * 5000.0), 1.0], [1.0 / (2 * np.pi * 10000.0), 1.0], FS)
    shelved = sp.lfilter(b, a, s, axis=0)

    deviation = response.sweep_deviation(s, shelved, FS)

    assert 4.0 < deviation < 6.5


def test_third_octave_response_reports_each_band() -> None:
    s = signals.log_sweep(FS, f1=20.0, f2=20000.0, level_dbfs=-30.0, seconds=10.0).samples

    centres, levels = response.third_octave_response(s, s * 0.5, FS)

    assert centres[0] == pytest.approx(25.0, rel=0.03)
    assert centres[-1] == pytest.approx(16000.0, rel=0.03)
    np.testing.assert_allclose(levels, -6.02, atol=0.01)
