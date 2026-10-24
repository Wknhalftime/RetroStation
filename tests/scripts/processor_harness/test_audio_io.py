"""WAV files in and out: 32-bit float, (samples, channels) arrays."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io


def test_float_wav_round_trips_exactly(tmp_path: Path) -> None:
    x = np.random.default_rng(1).uniform(-1.0, 1.0, (1000, 2)).astype(np.float32)

    audio_io.write_wav(tmp_path / "a.wav", x.astype(np.float64), 44100)
    y, fs = audio_io.read_wav(tmp_path / "a.wav")

    assert fs == 44100
    assert y.dtype == np.float64
    np.testing.assert_array_equal(y, x.astype(np.float64))


def test_mono_reads_as_one_column(tmp_path: Path) -> None:
    audio_io.write_wav(tmp_path / "m.wav", np.zeros((10, 1)), 44100)

    y, _ = audio_io.read_wav(tmp_path / "m.wav")

    assert y.shape == (10, 1)


def test_reading_a_missing_file_names_it(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="nope.wav"):
        audio_io.read_wav(tmp_path / "nope.wav")
