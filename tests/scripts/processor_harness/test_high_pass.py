"""The chain's first stage (Lance, 2026-10-25): a Butterworth high-pass near 30 Hz to take out
rumble and DC offset. Its response must be the Butterworth one."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, render, signals
from processor_harness.metrics import response
from processor_harness.presets import ParamValue
from scipy import signal as sp

FS = 44100
LIQUIDSOAP = os.environ.get("LIQUIDSOAP_PATH") or shutil.which("liquidsoap")
CHAIN = Path(__file__).resolve().parents[3] / "scripts/processor_harness/chains/high_pass.liq"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.timeout(240),
    pytest.mark.skipif(LIQUIDSOAP is None, reason="LIQUIDSOAP_PATH unset"),
]


def through(tmp_path: Path, params: dict[str, ParamValue]) -> tuple[np.ndarray, np.ndarray]:
    assert LIQUIDSOAP is not None
    stim = signals.impulse(FS, -6.0, 3.0, 0.5)
    audio_io.write_wav(tmp_path / "in.wav", stim.samples, FS)
    run = render.render_liquidsoap(
        Path(LIQUIDSOAP), CHAIN, params, tmp_path / "in.wav", tmp_path / "out.wav", tmp_path / "c"
    )
    out, _ = audio_io.read_wav(run.output)
    padded = np.zeros_like(stim.samples)
    padded[: min(len(out), len(padded))] = out[: len(padded)]
    return stim.samples, padded


@pytest.mark.parametrize(("order", "hz"), [(2, 30.0), (4, 30.0), (8, 30.0), (1, 10.0), (3, 30.0)])
def test_the_high_pass_is_a_butterworth(tmp_path: Path, order: int, hz: float) -> None:
    inp, out = through(tmp_path, {"hp_hz": hz, "hp_order": order})
    _, h = sp.sosfreqz(sp.butter(order, hz, "highpass", fs=FS, output="sos"), [20.0, 40.0], fs=FS)

    assert response.response_at_db(inp, out, FS, 40.0) == pytest.approx(
        20 * np.log10(abs(h[1])), abs=0.05
    )
    assert response.response_at_db(inp, out, FS, 20.0) == pytest.approx(
        20 * np.log10(abs(h[0])), abs=0.3
    )
