"""The limiter's gain reduction, from two renders of one input: the chain with the limiter and
the same chain with the limiter off (limiter step; Lance, 2026-10-26: the limiter catches
overs only, and the least p95 gain reduction wins at the qualifying ceiling).

The renders are lined up by cross-correlation (an oversampled limiter's resamplers add a few
samples), then compared in 5 ms blocks wherever the reference is sounding.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from scipy.signal import correlate

from processor_harness.audio_io import Audio, read_wav

BLOCK_S = 0.005
FLOOR_DBFS = -60.0  # quieter blocks have nothing for a limiter to do
ACTIVE_DB = 0.1  # a block counts as limited when the limiter takes this much off
MIN_SOUNDING_S = 1.0  # less sound than this (a test click) says nothing about a limiter
_LAG_SPAN_S = 30.0
_MAX_LAG_S = 0.05


@dataclass(frozen=True)
class GainReduction:
    p50_db: float
    p95_db: float
    max_db: float
    active_pct: float  # share of sounding blocks the limiter acted on


def _lag(reference: Audio, limited: Audio, fs: int) -> int:
    """How many samples the limited render runs behind the reference."""
    n = min(len(reference), len(limited), round(_LAG_SPAN_S * fs))
    c = correlate(limited[:n, 0], reference[:n, 0], mode="full", method="fft")
    centre = n - 1
    reach = round(_MAX_LAG_S * fs)
    window = c[centre - reach : centre + reach + 1]
    return int(np.argmax(window)) - reach


def _block_db(x: Audio, block: int) -> NDArray[np.float64]:
    count = len(x) // block
    power = np.mean(x[: count * block] ** 2, axis=1).reshape(count, block).mean(axis=1)
    return np.asarray(10 * np.log10(np.maximum(power, 1e-30)), dtype=np.float64)


def gain_reduction(reference: Audio, limited: Audio, fs: int) -> GainReduction:
    """The limiter's gain reduction over the sounding blocks of one input."""
    lag = _lag(reference, limited, fs)
    if lag > 0:
        limited = limited[lag:]
    elif lag < 0:
        reference = reference[-lag:]
    n = min(len(reference), len(limited))
    block = round(BLOCK_S * fs)
    ref_db, lim_db = _block_db(reference[:n], block), _block_db(limited[:n], block)
    gr = (ref_db - lim_db)[ref_db >= FLOOR_DBFS]
    if gr.size * BLOCK_S < MIN_SOUNDING_S:
        return GainReduction(math.nan, math.nan, math.nan, math.nan)
    return GainReduction(
        p50_db=float(np.percentile(gr, 50)),
        p95_db=float(np.percentile(gr, 95)),
        max_db=float(np.max(gr)),
        active_pct=100 * float(np.mean(gr > ACTIVE_DB)),
    )


def compare_runs(reference_dir: Path, variant_dir: Path) -> dict[str, dict[str, float]]:
    """Per input rendered in both run directories (a preset's folder in a harness run), the
    variant's limiter gain reduction against the reference. Inputs with under a second of
    sound (the run's impulse) are left out."""
    rows = {}
    for path in sorted(variant_dir.glob("*.wav")):
        name = path.stem
        if name.startswith("_") or name.endswith(".decoded"):
            continue
        ref_path = reference_dir / path.name
        if not ref_path.is_file():
            continue
        ref, fs = read_wav(ref_path)
        gr = gain_reduction(ref, read_wav(path)[0], fs)
        if math.isnan(gr.p50_db):
            continue  # under a second of sound: nothing to say about the limiter
        rows[name] = {
            "lim_gr_p50_db": gr.p50_db,
            "lim_gr_p95_db": gr.p95_db,
            "lim_gr_max_db": gr.max_db,
            "lim_active_pct": gr.active_pct,
        }
    return rows
