"""Pre-ringing around a transient: energy before the onset, how far before it the output is
within 60 dB of the transient's peak, and its level once more than 20 ms early."""

from __future__ import annotations

import numpy as np
import pytest
from processor_harness import signals
from processor_harness.metrics import transients

FS = 44100


def test_a_clean_render_has_no_pre_ring() -> None:
    hit = signals.kick(FS, level_dbfs=-6.0, seconds=1.5, at_s=0.5).samples

    ring = transients.pre_ring(hit, hit, FS, latency_s=0.0)

    assert ring.energy_db == float("-inf")
    assert ring.reach_ms == 0.0
    assert ring.beyond_20ms_db == float("-inf")


def test_an_echo_30_ms_early_at_minus_40_db_is_found() -> None:
    hit = signals.kick(FS, level_dbfs=-6.0, seconds=1.5, at_s=0.5).samples
    early = round(0.030 * FS)
    out = hit + 0.01 * np.roll(hit, -early, axis=0)  # -40 dB copy, 30 ms ahead

    ring = transients.pre_ring(hit, out, FS, latency_s=0.0)

    assert ring.reach_ms == pytest.approx(30.0, abs=1.0)
    assert ring.beyond_20ms_db == pytest.approx(-40.0, abs=1.5)
    assert ring.energy_db < -30.0


def test_the_output_is_lined_up_by_the_chains_latency() -> None:
    hit = signals.snare(FS, level_dbfs=-6.0, seconds=1.5, at_s=0.5).samples
    early = round(0.010 * FS)
    out = hit + 0.01 * np.roll(hit, -early, axis=0)
    delayed = np.roll(out, round(0.050 * FS), axis=0)  # a 50 ms linear-phase chain

    ring = transients.pre_ring(hit, delayed, FS, latency_s=0.050)

    assert ring.reach_ms == pytest.approx(10.0, abs=1.0)
    assert ring.beyond_20ms_db == float("-inf")


def test_onset_is_the_first_sample_within_30_db_of_the_peak() -> None:
    x = np.zeros((FS, 2))
    x[1000, :] = 0.01  # -40 dB re peak: not yet the onset
    x[2000, :] = 0.05  # -26 dB re peak: the onset
    x[2005, :] = 1.0

    assert transients.onset(x) == 2000


def test_kick_and_snare_start_from_silence_at_their_level() -> None:
    for make in (signals.kick, signals.snare):
        s = make(FS, level_dbfs=-6.0, seconds=1.5, at_s=0.5)
        start = round(0.5 * FS)
        assert not np.any(s.samples[:start])
        assert 20 * np.log10(np.max(np.abs(s.samples))) == pytest.approx(-6.0, abs=0.01)
        assert s.segments[0].start_s == 0.5


def test_kick_is_bass_and_snare_is_broadband() -> None:
    def share_above(x: np.ndarray, hz: float) -> float:
        power = np.abs(np.fft.rfft(x[:, 0])) ** 2
        f = np.fft.rfftfreq(len(x), 1 / FS)
        return float(np.sum(power[f > hz]) / np.sum(power))

    kick = signals.kick(FS, level_dbfs=-6.0, seconds=1.5, at_s=0.5).samples
    snare = signals.snare(FS, level_dbfs=-6.0, seconds=1.5, at_s=0.5).samples

    assert share_above(kick, 200.0) < 0.05
    assert share_above(snare, 2000.0) > 0.2
