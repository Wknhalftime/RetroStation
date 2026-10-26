"""R4: the harness's own loudness maths, checked against an independent meter (FFmpeg's ebur128
filter) on a programme-like signal at the stream's rate."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, signals
from processor_harness.metrics import loudness

FS = 44100
FFMPEG = shutil.which("ffmpeg")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH"),
]


def ebur128_summary(path: Path) -> dict[str, float]:
    """FFmpeg's ebur128 summary: integrated loudness, LRA and true peak."""
    assert FFMPEG is not None
    run = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "ebur128=peak=true:framelog=quiet",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    summary = run.stderr[run.stderr.rindex("Summary:") :]

    def value(label: str) -> float:
        match = re.search(rf"{label}:\s+(-?[\d.]+|-inf)", summary)
        assert match is not None, label
        return float(match.group(1))

    return {"I": value("I"), "LRA": value("LRA"), "Peak": value("Peak")}


def test_integrated_lra_and_true_peak_agree_with_ffmpeg_ebur128(tmp_path: Path) -> None:
    # Pink-noise bursts at three levels with pauses: exercises both gates and the LRA.
    parts = [
        signals.noise_with_pauses(FS, burst_s=8.0, pause_s=2.0, bursts=1, level_dbfs=level)
        for level in (-18.0, -28.0, -22.0)
    ]
    x = np.concatenate([p.samples for p in parts])
    path = tmp_path / "program.wav"
    audio_io.write_wav(path, x, FS)
    x, _ = audio_io.read_wav(path)

    reference = ebur128_summary(path)

    assert loudness.integrated(x, FS) == pytest.approx(reference["I"], abs=0.1)
    assert loudness.loudness_range(x, FS) == pytest.approx(reference["LRA"], abs=0.5)
    assert loudness.true_peak(x, FS) == pytest.approx(reference["Peak"], abs=0.2)
