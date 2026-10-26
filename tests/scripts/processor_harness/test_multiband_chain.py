"""The band step's chain: the R6 linear-phase crossover with one acompressor per band, each band
taking its own value from the per-band params (Lance, 2026-10-25)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from processor_harness import audio_io, render, signals
from processor_harness.metrics import fits
from processor_harness.metrics.acompressor import Settings
from processor_harness.presets import ParamValue

FS = 44100
LIQUIDSOAP = os.environ.get("LIQUIDSOAP_PATH") or shutil.which("liquidsoap")
CHAIN = (
    Path(__file__).resolve().parents[3] / "scripts/processor_harness/chains/multiband_linear.liq"
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.timeout(240),
    pytest.mark.skipif(LIQUIDSOAP is None, reason="LIQUIDSOAP_PATH unset"),
]


def test_each_band_takes_its_own_compressor_settings(tmp_path: Path) -> None:
    # A 1 kHz stepped tone sits in band 3 of 100 / 520 / 1800 / 6000. Only band 3's settings
    # may shape it: its neighbours are set to other thresholds and ratios.
    assert LIQUIDSOAP is not None
    stim = signals.stepped_tone(FS, 1000.0, -40.0, 0.0, hold_s=0.6)
    audio_io.write_wav(tmp_path / "in.wav", stim.samples, FS)
    params: dict[str, ParamValue] = {
        "split": "100 520 1800 6000",
        "threshold_db": "-10 -35 -24 -30 -12",
        "ratio": "8 6 3 5 7",
        "knee_db": "9.03 9.03 9.03 9.03 9.03",
        "makeup_db": "0 0 0 0 0",
        "attack_ms": "34 33 20 25 25",
        "release_ms": "250 250 250 250 250",
        "detection": 1,
        "link": 0,
    }
    run = render.render_liquidsoap(
        Path(LIQUIDSOAP), CHAIN, params, tmp_path / "in.wav", tmp_path / "out.wav", tmp_path / "c"
    )
    out, _ = audio_io.read_wav(run.output)
    band3 = Settings.from_db(-24.0, 3.0, 9.03, 0.0, 20.0, 250.0)

    fit = fits.fit_acompressor_static(stim.segments, stim.samples, out, FS, band3, 0.02)

    assert fit.threshold_db == pytest.approx(-24.0, abs=0.3)
    assert fit.ratio == pytest.approx(3.0, rel=0.05)
