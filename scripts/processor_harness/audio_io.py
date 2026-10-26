"""WAV in and out. The harness keeps audio as (samples, channels) float64 at full scale 1.0 and
writes 32-bit float files, so renders never clip or dither on the way to the measurements."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from scipy.io import wavfile

Audio = NDArray[np.float64]

_INT_FULL_SCALE = {np.dtype(np.int16): 32768.0, np.dtype(np.int32): 2147483648.0}


def write_wav(path: Path, x: Audio, fs: int) -> None:
    """Write a 32-bit float WAV."""
    wavfile.write(path, fs, np.asarray(x, dtype=np.float32))


def read_wav(path: Path) -> tuple[Audio, int]:
    """Read a float or 16/32-bit integer WAV as (samples, channels) float64 and its rate."""
    if not path.is_file():
        raise FileNotFoundError(f"WAV file not found: {path}")
    fs, data = wavfile.read(path)
    scale = _INT_FULL_SCALE.get(data.dtype, 1.0)
    x = np.asarray(data, dtype=np.float64) / scale
    if x.ndim == 1:
        x = x[:, np.newaxis]
    return x, int(fs)
