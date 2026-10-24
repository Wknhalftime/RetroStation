"""The synthetic stimuli: each one carries the schedule its analysis needs, and its levels are
what the schedule says."""

from __future__ import annotations

import numpy as np
import pytest
from processor_harness import signals

FS = 44100


def peak_db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.max(np.abs(x))))


def rms_db(x: np.ndarray) -> float:
    return float(10 * np.log10(np.mean(x * x)))


def band_energy_db(x: np.ndarray, lo: float, hi: float) -> float:
    spectrum = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(len(x), 1 / FS)
    return float(10 * np.log10(np.sum(spectrum[(f >= lo) & (f < hi)])))


def test_stimuli_are_stereo_with_identical_channels() -> None:
    s = signals.tone(FS, freq=1000.0, level_dbfs=-20.0, seconds=1.0)

    assert s.samples.shape == (FS, 2)
    np.testing.assert_array_equal(s.samples[:, 0], s.samples[:, 1])


def test_impulse_is_one_sample_at_the_given_level_and_time() -> None:
    s = signals.impulse(FS, level_dbfs=-30.0, seconds=2.0, at_s=0.5)

    nonzero = np.flatnonzero(s.samples[:, 0])
    assert list(nonzero) == [round(0.5 * FS)]
    assert peak_db(s.samples) == pytest.approx(-30.0, abs=1e-9)


def test_log_sweep_spreads_equal_energy_per_octave_at_the_given_peak() -> None:
    s = signals.log_sweep(FS, f1=20.0, f2=20000.0, level_dbfs=-30.0, seconds=10.0)
    mono = s.samples[:, 0]

    assert peak_db(mono) == pytest.approx(-30.0, abs=0.01)
    low = band_energy_db(mono, 100.0, 200.0)
    high = band_energy_db(mono, 2000.0, 4000.0)
    assert low == pytest.approx(high, abs=0.5)


def test_stepped_tone_holds_each_2_db_step_from_minus_40_to_0() -> None:
    s = signals.stepped_tone(FS, freq=1000.0, start_dbfs=-40.0, stop_dbfs=0.0, hold_s=1.0)

    levels = [seg.level_dbfs for seg in s.segments]
    assert levels == [float(v) for v in range(-40, 1, 2)]
    for seg in s.segments:
        part = s.samples[round(seg.start_s * FS) : round(seg.stop_s * FS), 0]
        assert peak_db(part) == pytest.approx(seg.level_dbfs, abs=0.05)


def test_tone_bursts_step_up_then_down_by_10_db() -> None:
    s = signals.tone_bursts(FS, freq=1000.0, base_dbfs=-30.0, step_db=10.0, hold_s=1.0, cycles=2)

    assert [seg.level_dbfs for seg in s.segments] == [-30.0, -20.0, -30.0, -20.0, -30.0]
    assert s.segments[1].start_s == pytest.approx(1.0)
    assert len(s.samples) == 5 * FS


def test_smpte_twin_tone_has_its_low_tone_12_db_above_the_high_one() -> None:
    s = signals.twin_tone(FS, f1=60.0, f2=7000.0, ratio=4.0, level_dbfs=-6.0, seconds=2.0)
    mono = s.samples[:, 0]

    assert peak_db(mono) == pytest.approx(-6.0, abs=0.05)
    diff = band_energy_db(mono, 55.0, 65.0) - band_energy_db(mono, 6990.0, 7010.0)
    assert diff == pytest.approx(12.04, abs=0.1)


def test_noise_with_pauses_is_silent_in_the_pauses_and_at_level_in_the_bursts() -> None:
    s = signals.noise_with_pauses(FS, burst_s=2.0, pause_s=3.0, bursts=2, level_dbfs=-20.0)

    pauses = [seg for seg in s.segments if seg.level_dbfs == float("-inf")]
    bursts = [seg for seg in s.segments if seg.level_dbfs != float("-inf")]
    assert len(pauses) == 2 and len(bursts) == 2
    for seg in pauses:
        part = s.samples[round(seg.start_s * FS) : round(seg.stop_s * FS)]
        assert not np.any(part)
    for seg in bursts:
        part = s.samples[round(seg.start_s * FS) : round(seg.stop_s * FS), 0]
        assert rms_db(part) == pytest.approx(-20.0, abs=0.1)


def test_noise_is_the_same_on_every_run() -> None:
    a = signals.noise_with_pauses(FS, burst_s=1.0, pause_s=1.0, bursts=1, level_dbfs=-20.0)
    b = signals.noise_with_pauses(FS, burst_s=1.0, pause_s=1.0, bursts=1, level_dbfs=-20.0)

    np.testing.assert_array_equal(a.samples, b.samples)
