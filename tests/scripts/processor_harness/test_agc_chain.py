"""The AGC-only chain re-arms agc.liq's intro hold at each playlist song start, as session.liq
does when a song starts, so a playlist render shows the AGC's real behaviour across songs."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, render, signals
from processor_harness.measure import read_gauges
from processor_harness.presets import ParamValue

FS = 44100
LIQUIDSOAP = os.environ.get("LIQUIDSOAP_PATH") or shutil.which("liquidsoap")
CHAIN = Path(__file__).resolve().parents[3] / "scripts/processor_harness/chains/agc_only.liq"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.timeout(240),
    pytest.mark.skipif(LIQUIDSOAP is None, reason="LIQUIDSOAP_PATH unset"),
]


def gain_rise_after_boundary(tmp_path: Path, boundaries: str) -> float:
    """A hot song (-12 LUFS) then a cold one (-24): how far the gain rises in the 5 s after the
    second song starts."""
    assert LIQUIDSOAP is not None
    tmp_path.mkdir(parents=True, exist_ok=True)
    hot = signals.noise_with_pauses(FS, burst_s=25.0, pause_s=0.01, bursts=1, level_dbfs=-14.0)
    cold = signals.noise_with_pauses(FS, burst_s=25.0, pause_s=0.01, bursts=1, level_dbfs=-26.0)
    x = np.concatenate([hot.samples, cold.samples])
    audio_io.write_wav(tmp_path / "in.wav", x, FS)
    boundary = len(hot.samples) / FS
    params: dict[str, ParamValue] = {"target": -18.0, "idle": 0.0}
    if boundaries:
        params["boundaries"] = f"0.0 {boundary}"
    run = render.render_liquidsoap(
        Path(LIQUIDSOAP), CHAIN, params, tmp_path / "in.wav", tmp_path / "out.wav", tmp_path / "c"
    )
    assert run.gauges is not None
    times, gain = read_gauges(run.gauges)["agc_gain"]
    at = float(np.interp(boundary, times, gain))
    return float(np.interp(boundary + 5.0, times, gain)) - at


def test_the_intro_hold_is_rearmed_at_each_playlist_song(tmp_path: Path) -> None:
    held = gain_rise_after_boundary(tmp_path / "held", boundaries="yes")
    free = gain_rise_after_boundary(tmp_path / "free", boundaries="")

    # Held: the gain only creeps up at the window rate (0.5 dB/s, 2.5 dB in 5 s). Free: it
    # rises at the release rate (3 dB/s).
    assert held <= 3.0
    assert free > held + 1.0
