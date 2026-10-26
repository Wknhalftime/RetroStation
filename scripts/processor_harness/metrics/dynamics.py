"""Dynamics (brief math item 10): crest factor, PLR, PSR, and samples over the ceiling."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from processor_harness.metrics import loudness

Audio = NDArray[np.float64]

# Short-term windows are 3 s and step 100 ms, so window k spans 100 ms blocks k .. k + 29.
_BLOCKS_PER_SHORT_TERM = round(loudness.SHORT_TERM_S / loudness.STEP_S)


def crest_factor(x: Audio) -> float:
    """Sample peak over RMS, all channels together, dB."""
    rms = float(np.sqrt(np.mean(x * x)))
    return loudness.to_db(float(np.max(np.abs(x)))) - loudness.to_db(rms)


def plr(x: Audio, fs: int) -> float:
    """Peak-to-loudness ratio: maximum true peak minus integrated loudness, LU."""
    return loudness.true_peak(x, fs) - loudness.integrated(x, fs)


def psr(x: Audio, fs: int) -> NDArray[np.float64]:
    """Peak-to-short-term-loudness ratio per short-term window (true peak in the window minus
    its short-term loudness), skipping windows below the -70 LUFS absolute gate."""
    short = loudness.short_term(x, fs)
    blocks = loudness.true_peak_blocks(x, fs)
    values = []
    for k, level in enumerate(short):
        if level <= loudness.ABSOLUTE_GATE_LUFS:
            continue
        peak = float(np.max(blocks[k : k + _BLOCKS_PER_SHORT_TERM]))
        values.append(loudness.to_db(peak) - float(level))
    return np.asarray(values, dtype=np.float64)


def sample_overs(x: Audio, ceiling_dbfs: float) -> int:
    """Samples (any channel) whose magnitude is above the ceiling."""
    return int(np.count_nonzero(np.abs(x) > 10 ** (ceiling_dbfs / 20)))


def true_overs(x: Audio, fs: int, ceiling_dbtp: float) -> int:
    """4x-oversampled samples (any channel) above the ceiling."""
    limit = 10 ** (ceiling_dbtp / 20)
    return sum(int(np.count_nonzero(up > limit)) for _, up in loudness.oversampled(x, fs))
