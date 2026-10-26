"""The full chain for the limiter step (Lance, 2026-10-26): DC blocker, AGC, multiband, then
makeup and alimiter. The limiter holds its ceiling on sample peaks; off, the made-up signal
passes over full scale (the float path carries it); oversampled, it also holds most of the
inter-sample peaks."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, render, signals
from processor_harness.metrics import loudness
from processor_harness.presets import ParamValue

FS = 44100
LIQUIDSOAP = os.environ.get("LIQUIDSOAP_PATH") or shutil.which("liquidsoap")
CHAIN = Path(__file__).resolve().parents[3] / "scripts/processor_harness/chains/full_chain.liq"
BASE: dict[str, ParamValue] = {
    "hp_hz": 5.0,
    "hp_order": 1,
    "split": "100 520 1800 6000",
    "threshold_db": "0 0 0 0 0",  # bands idle: only the limiter shapes the peaks
    "ratio": "1 1 1 1 1",
    "out_makeup_db": 12.0,
    "limit_db": -1.0,
}

pytestmark = [
    pytest.mark.slow,
    pytest.mark.timeout(300),
    pytest.mark.skipif(LIQUIDSOAP is None, reason="LIQUIDSOAP_PATH unset"),
]


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("full") / "in.wav"
    audio_io.write_wav(path, signals.noise_with_pauses(FS, 6.0, 0.0, 1, -18.0).samples, FS)
    return path


def rendered(source: Path, tmp_path: Path, extra: dict[str, ParamValue]) -> np.ndarray:
    assert LIQUIDSOAP is not None
    tmp_path.mkdir(parents=True, exist_ok=True)
    run = render.render_liquidsoap(
        Path(LIQUIDSOAP), CHAIN, BASE | extra, source, tmp_path / "out.wav", tmp_path / "c"
    )
    out, _ = audio_io.read_wav(run.output)
    return out[FS:]  # past the AGC's first second


def test_the_limiter_holds_its_ceiling_on_sample_peaks(source: Path, tmp_path: Path) -> None:
    out = rendered(source, tmp_path, {})

    assert 20 * np.log10(np.max(np.abs(out))) <= -0.95


def test_without_the_limiter_the_made_up_signal_passes_full_scale(
    source: Path, tmp_path: Path
) -> None:
    out = rendered(source, tmp_path, {"limiter": 0})

    assert np.max(np.abs(out)) > 1.0


def test_oversampled_the_limiter_holds_more_of_the_inter_sample_peaks(
    source: Path, tmp_path: Path
) -> None:
    plain = rendered(source, tmp_path / "plain", {})
    oversampled = rendered(source, tmp_path / "x4", {"oversample": 4})

    assert loudness.true_peak(oversampled, FS) < loudness.true_peak(plain, FS)
    assert loudness.true_peak(oversampled, FS) <= -0.7
