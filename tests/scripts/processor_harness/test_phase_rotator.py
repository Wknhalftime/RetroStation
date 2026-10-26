"""The phase-rotator step's chain (math item 8, Lance 2026-10-25): an FFmpeg allpass cascade,
first order at staggered corners or second order stacked at one corner. It must leave every
level alone and only turn the phase."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, render, signals
from processor_harness.metrics import response
from processor_harness.presets import ParamValue

FS = 44100
LIQUIDSOAP = os.environ.get("LIQUIDSOAP_PATH") or shutil.which("liquidsoap")
CHAIN = Path(__file__).resolve().parents[3] / "scripts/processor_harness/chains/phase_rotator.liq"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.timeout(240),
    pytest.mark.skipif(LIQUIDSOAP is None, reason="LIQUIDSOAP_PATH unset"),
]


def through(tmp_path: Path, params: dict[str, ParamValue]) -> tuple[np.ndarray, np.ndarray]:
    assert LIQUIDSOAP is not None
    stim = signals.impulse(FS, -6.0, 2.0, 0.5)
    audio_io.write_wav(tmp_path / "in.wav", stim.samples, FS)
    run = render.render_liquidsoap(
        Path(LIQUIDSOAP), CHAIN, params, tmp_path / "in.wav", tmp_path / "out.wav", tmp_path / "c"
    )
    out, _ = audio_io.read_wav(run.output)
    # A render ends with its input, a few frames short; the impulse's tail is long gone by then.
    padded = np.zeros_like(stim.samples)
    padded[: min(len(out), len(padded))] = out[: len(padded)]
    return stim.samples, padded


def phase_turn_deg(inp: np.ndarray, out: np.ndarray, lo: float, hi: float) -> float:
    """How far the phase turns between two frequencies (degrees, unwrapped)."""
    freqs = np.fft.rfftfreq(len(inp), 1 / FS)
    h = np.fft.rfft(out[:, 0]) / np.fft.rfft(inp[:, 0])
    span = (freqs >= lo) & (freqs <= hi)
    phase = np.unwrap(np.angle(h[span]))
    return float(np.degrees(abs(phase[-1] - phase[0])))


@pytest.mark.parametrize(
    "params",
    [
        {"corners": "100 160 250 400", "order": 1},
        {"corners": "150 150", "order": 2, "q": 0.707},
    ],
)
def test_the_rotator_turns_the_phase_and_leaves_every_level(
    tmp_path: Path, params: dict[str, ParamValue]
) -> None:
    inp, out = through(tmp_path, params)

    low, high = response.band_sum_range(inp, out, FS)
    assert abs(low) < 0.1 and abs(high) < 0.1  # flat within 0.1 dB, 20 Hz to 20 kHz
    assert phase_turn_deg(inp, out, 20.0, 2000.0) > 360.0


def test_no_corners_is_no_rotation(tmp_path: Path) -> None:
    inp, out = through(tmp_path, {"corners": ""})

    assert np.max(np.abs(out - inp)) < 1e-6
