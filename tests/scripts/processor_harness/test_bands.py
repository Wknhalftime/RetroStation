"""Per-band measurement for the band step (Lance, 2026-10-25): the R6 split copied into Python,
so a render's input and output can be split into the chain's bands and compared band by band
(each band's level, its gain reduction, how the balance between bands moves)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, render, signals
from processor_harness.metrics import acompressor, bands
from processor_harness.metrics.acompressor import Settings

FS = 44100
EDGES = (100.0, 520.0, 1800.0, 6000.0)
LIQUIDSOAP = os.environ.get("LIQUIDSOAP_PATH") or shutil.which("liquidsoap")
CHAINS = Path(__file__).resolve().parents[3] / "scripts/processor_harness/chains"


def pink(seconds: float, level_dbfs: float = -20.0, seed: int = 0) -> np.ndarray:
    s = signals.noise_with_pauses(FS, seconds, 0.0, 1, level_dbfs, seed=seed)
    return s.samples


def scaled_band(x: np.ndarray, band: int, gain_db: np.ndarray | float) -> np.ndarray:
    """x with one band scaled (a gain per sample, or one gain): the rest passes untouched."""
    k = bands.kernels(EDGES, FS)[band]
    part = np.stack([np.convolve(x[:, c], k, mode="same") for c in range(x.shape[1])], axis=1)
    g = 10 ** (np.asarray(gain_db) / 20)
    g = g[:, np.newaxis] if np.ndim(g) else g
    return np.asarray(x + part * (g - 1), dtype=np.float64)


def test_the_bands_add_up_to_the_input() -> None:
    # Brick-wall gains that partition the spectrum, one window: the kernels sum to an impulse.
    k = bands.kernels(EDGES, FS)

    total = k.sum(axis=0)
    centre = len(total) // 2
    assert total[centre] == pytest.approx(1.0, abs=1e-12)
    assert np.max(np.abs(np.delete(total, centre))) < 1e-12


def test_a_tone_lands_in_its_own_band() -> None:
    x = signals.tone(FS, 1000.0, -20.0, 4.0).samples

    powers = bands.band_powers(x, FS, EDGES)
    level = 10 * np.log10(powers.mean(axis=1))

    assert level[2] == pytest.approx(-23.01, abs=0.05)  # -20 dBFS peak is -23 dB mean power
    assert np.all(np.delete(level, 2) < level[2] - 40)


def test_a_band_turned_down_shows_as_that_bands_gain_reduction() -> None:
    x = pink(8.0)
    out = scaled_band(x, 2, -3.0)

    c = bands.compare(x, out, FS, EDGES, latency_s=0.0)

    assert c.gr_mean_db[2] == pytest.approx(3.0, abs=0.05)
    assert np.allclose(np.delete(c.gr_mean_db, 2), 0.0, atol=0.05)
    assert c.out_db[2] == pytest.approx(c.in_db[2] - 3.0, abs=0.05)


def test_the_output_is_lined_up_by_the_latency() -> None:
    x = pink(8.0)
    late = np.concatenate([np.zeros((441, 2)), scaled_band(x, 2, -3.0)])[: len(x)]

    c = bands.compare(x, late, FS, EDGES, latency_s=441 / FS)

    assert c.gr_mean_db[2] == pytest.approx(3.0, abs=0.1)
    assert abs(c.gr_mean_db[1]) < 0.1


def test_a_band_full_of_crossover_transition_reads_its_cut_a_little_short() -> None:
    # Splitting the output again is not exact inside each edge's transition (about 100 Hz wide
    # at 20 ms), so a band whose energy sits there reads its gain reduction about 5% short:
    # band 2 (100-520 Hz) on pink noise reads a 3 dB cut as about 2.85 dB.
    x = pink(8.0)

    c = bands.compare(x, scaled_band(x, 1, -3.0), FS, EDGES, latency_s=0.0)

    assert 2.7 < c.gr_mean_db[1] < 3.0


def test_a_steady_cut_changes_the_balance_without_drifting() -> None:
    x = pink(8.0)

    c = bands.compare(x, scaled_band(x, 3, -6.0), FS, EDGES, latency_s=0.0)

    assert c.change_db > 1.0
    assert c.drift_db < 0.1


def test_a_band_that_moves_during_the_song_drifts() -> None:
    x = pink(8.0)
    t = np.arange(len(x)) / FS
    pumping = np.where((t // 2) % 2 == 0, 0.0, -6.0)  # band 4 cut by 6 dB every other 2 s

    c = bands.compare(x, scaled_band(x, 3, pumping), FS, EDGES, latency_s=0.0)

    assert c.drift_db > 1.0


def test_silence_is_left_out_of_every_band_level() -> None:
    x = np.concatenate([pink(4.0), np.zeros((4 * FS, 2))])

    c = bands.compare(x, x, FS, EDGES, latency_s=0.0)

    sounding = bands.band_powers(x[: 4 * FS], FS, EDGES)[2].mean()
    assert c.in_db[2] == pytest.approx(10 * np.log10(sounding), abs=0.3)


def test_thresholds_are_found_for_a_target_gain_reduction() -> None:
    # Calibrate band 3, then run the exact acompressor model on that band with the threshold
    # found: the gain reduction measured on the result is the target.
    x = np.concatenate(
        [pink(6.0, -14.0, seed=1), pink(6.0, -26.0, seed=2), pink(6.0, -18.0, seed=3)]
    )
    s = Settings.from_db(0.0, 2.5, 9.03, 0.0, 33.0, 1000.0)

    traces = [bands.calibration_traces(x, FS, EDGES, [s] * 5)]
    threshold_db = bands.thresholds_for_gain_reduction(traces, [s] * 5, 3.0)[2]

    k = bands.kernels(EDGES, FS)[2]
    part = np.stack([np.convolve(x[:, c], k, mode="same") for c in range(2)], axis=1)
    tuned = Settings.from_db(threshold_db, 2.5, 9.03, 0.0, 33.0, 1000.0)
    out = x - part + acompressor.process(part, FS, tuned)
    measured = bands.compare(x, out, FS, EDGES, latency_s=0.0).gr_mean_db[2]
    assert measured == pytest.approx(3.0, abs=0.3)


@pytest.mark.slow
@pytest.mark.timeout(240)
@pytest.mark.skipif(LIQUIDSOAP is None, reason="LIQUIDSOAP_PATH unset")
def test_the_python_split_matches_the_chains_crossover(tmp_path: Path) -> None:
    # Each band rendered alone through crossover_linear.liq against the same band split here.
    assert LIQUIDSOAP is not None
    x = pink(6.0)
    audio_io.write_wav(tmp_path / "in.wav", x, FS)
    ours = bands.band_powers(x, FS, EDGES)
    for band in range(len(EDGES) + 1):
        run = render.render_liquidsoap(
            Path(LIQUIDSOAP),
            CHAINS / "crossover_linear.liq",
            {"split": " ".join(str(e) for e in EDGES), "solo": band + 1},
            tmp_path / "in.wav",
            tmp_path / f"band{band}.wav",
            tmp_path / "cache",
        )
        y, _ = audio_io.read_wav(run.output)
        theirs = 10 * np.log10(np.mean(y[FS : 5 * FS] ** 2))
        here = 10 * np.log10(ours[band][2:-2].mean())  # the same stretch, 0.8-5.2 s
        # firequalizer designs its FIRs a little differently: its bottom bands read up to
        # 0.11 dB lower (measured 2026-10-25: -0.11, -0.09, -0.03, 0.00, 0.00 dB).
        assert theirs == pytest.approx(here, abs=0.15), f"band {band + 1}"
