"""The chain's bands, measured in Python: the R6 split copied (brick-wall gains, one Hann window,
one delay), so a render's input and output can be compared band by band. The band step
(Lance, 2026-10-25) chooses edges by the balance drift at equal gain reduction, with the
loudness-range cost as the tiebreak, and wants each band's output level consistent between songs.

- Levels are mean power per 400 ms block, both channels together, in dB.
- Blocks where the input is silent (under -60 dBFS) are left out of everything; a block also
  drops out of a band's gain reduction while that band is under -70 dBFS (nothing sounding).
- Gain reduction per block is the band's input level minus its output level. Splitting the
  output again is not exact inside each edge's transition (about 100 Hz wide at 20 ms), so
  a band with much of its energy there reads short: band 2 (100-520 Hz) on pink noise
  reads a 3 dB cut as 2.85 dB. Threshold calibration does not split again, so in such a band
  the render measures a little under the target; the render is what counts.
- Balance drift: how far the bands' gain reductions move apart and together during the song,
  with the moment's overall gain and each band's average taken out (dB RMS). Balance change:
  how far the song's average balance moved (the spread of the bands' average reductions).
- Octave levels (31.5 Hz to 16 kHz) put every edge set on one grid, so each song's balance
  can be compared between songs whatever the bands are.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np
from numpy.typing import NDArray
from scipy.signal import oaconvolve, welch

from processor_harness.metrics import acompressor
from processor_harness.metrics.acompressor import Settings

Audio = NDArray[np.float64]

DELAY_S = 0.02  # the crossover's FIR half-length, locked in dial-in step 1
BLOCK_S = 0.4
SILENT_DBFS = -60.0  # a block this quiet is not programme
BAND_FLOOR_DBFS = -70.0  # a band this quiet in a block has nothing to compress
OCTAVE_CENTRES = 1000.0 * 2.0 ** np.arange(-5, 5)
OCTAVE_LABELS = ("31.5", "63", "125", "250", "500", "1000", "2000", "4000", "8000", "16000")


def kernels(edges: Sequence[float], fs: int, delay_s: float = DELAY_S) -> NDArray[np.float64]:
    """One FIR per band (rows), as crossover_linear.liq builds them: brick-wall gains that
    partition the spectrum, windowed by one Hann window, so the rows add up to an impulse."""
    n = round(delay_s * fs)
    t = np.arange(-n, n + 1)

    def lowpass(fc: float) -> NDArray[np.float64]:
        return np.asarray(2 * fc / fs * np.sinc(2 * fc * t / fs), dtype=np.float64)

    delta = (t == 0).astype(np.float64)
    cuts = [lowpass(e) for e in edges]
    rows = (
        [cuts[0]] + [hi - lo for lo, hi in zip(cuts, cuts[1:], strict=False)] + [delta - cuts[-1]]
    )
    return np.asarray(np.array(rows) * np.hanning(2 * n + 1), dtype=np.float64)


def _band(x: Audio, kernel: NDArray[np.float64]) -> Audio:
    return np.asarray(oaconvolve(x, kernel[:, np.newaxis], mode="same", axes=0), dtype=np.float64)


def _block_mean(power: NDArray[np.float64], block: int) -> NDArray[np.float64]:
    count = len(power) // block
    return np.asarray(power[: count * block].reshape(count, block).mean(axis=1), dtype=np.float64)


def band_powers(
    x: Audio, fs: int, edges: Sequence[float], block_s: float = BLOCK_S, delay_s: float = DELAY_S
) -> NDArray[np.float64]:
    """Mean power per block in each band (rows), both channels together."""
    block = round(block_s * fs)
    return np.array(
        [_block_mean(np.mean(_band(x, k) ** 2, axis=1), block) for k in kernels(edges, fs, delay_s)]
    )


def octave_levels(x: Audio, fs: int) -> NDArray[np.float64]:
    """Mean power in each octave band (dB), both channels together, over the whole input."""
    freqs, psd = welch(x, fs, nperseg=16384, axis=0)
    power = np.mean(psd, axis=1) * (freqs[1] - freqs[0])
    levels = [
        power[(freqs >= c / np.sqrt(2)) & (freqs < c * np.sqrt(2))].sum() for c in OCTAVE_CENTRES
    ]
    return _db(np.array(levels))


def _db(power: NDArray[np.float64]) -> NDArray[np.float64]:
    return np.asarray(10 * np.log10(np.maximum(power, 1e-30)), dtype=np.float64)


def _sounding(x: Audio, fs: int, block_s: float) -> NDArray[np.bool_]:
    """Blocks where the input is programme, not silence."""
    return _db(_block_mean(np.mean(x**2, axis=1), round(block_s * fs))) >= SILENT_DBFS


@dataclass(frozen=True)
class BandComparison:
    in_db: tuple[float, ...]  # each band's input level over the sounding blocks
    out_db: tuple[float, ...]
    gr_mean_db: tuple[float, ...]
    gr_p95_db: tuple[float, ...]
    drift_db: float
    change_db: float


def _aligned(out: Audio, length: int, latency_s: float, fs: int) -> Audio:
    lag = round(latency_s * fs)
    part = out[lag : lag + length]
    if len(part) < length:
        part = np.concatenate([part, np.zeros((length - len(part), out.shape[1]))])
    return np.asarray(part, dtype=np.float64)


def _mean_db(power: NDArray[np.float64]) -> float:
    return float(10 * np.log10(power.mean())) if power.size else math.nan


def compare(
    inp: Audio,
    out: Audio,
    fs: int,
    edges: Sequence[float],
    latency_s: float,
    block_s: float = BLOCK_S,
    delay_s: float = DELAY_S,
) -> BandComparison:
    """The input and the output (lined up by the chain's latency) compared band by band."""
    p_in = band_powers(inp, fs, edges, block_s, delay_s)
    p_out = band_powers(_aligned(out, len(inp), latency_s, fs), fs, edges, block_s, delay_s)
    sounding = _sounding(inp, fs, block_s)[: p_in.shape[1]]
    gr = _db(p_in) - _db(p_out)
    active = sounding & (_db(p_in) >= BAND_FLOOR_DBFS)
    pairs = list(zip(gr, active, strict=True))
    gr_mean = tuple(float(np.mean(g[a])) if a.any() else math.nan for g, a in pairs)
    gr_p95 = tuple(float(np.percentile(g[a], 95)) if a.any() else math.nan for g, a in pairs)
    balance = _balance(gr[:, active.all(axis=0)])
    return BandComparison(
        in_db=tuple(_mean_db(p[sounding]) for p in p_in),
        out_db=tuple(_mean_db(p[sounding]) for p in p_out),
        gr_mean_db=gr_mean,
        gr_p95_db=gr_p95,
        drift_db=balance.drift_db,
        change_db=balance.change_db,
    )


@dataclass(frozen=True)
class Balance:
    drift_db: float  # how far the bands moved apart and together during the song
    change_db: float  # how far the song's average balance moved


def _balance(gr: NDArray[np.float64]) -> Balance:
    """Balance drift and change from gain reductions (rows: bands, columns: blocks where every
    band is sounding)."""
    if gr.shape[1] < 2:
        return Balance(math.nan, math.nan)
    moment = gr - gr.mean(axis=0)  # the moment's overall gain taken out
    averages = moment.mean(axis=1, keepdims=True)
    return Balance(
        drift_db=float(np.sqrt(np.mean((moment - averages) ** 2))),
        change_db=float(np.sqrt(np.mean(averages**2))),
    )


_DRIFT_OCTAVES = slice(1, 9)  # 63 Hz to 8 kHz: 31.5 Hz and 16 kHz are empty in many songs


def _octave_block_powers(x: Audio, fs: int, block_s: float) -> NDArray[np.float64]:
    """Mean power per block in each octave band (rows), from a Hann-windowed FFT of each block,
    scaled so a band's sum is its share of the block's mean power."""
    block = round(block_s * fs)
    count = len(x) // block
    window = np.hanning(block)
    blocks = x[: count * block].reshape(count, block, x.shape[1]) * window[:, None]
    scale = 2 / (block * np.sum(window**2))
    spectrum = np.mean(np.abs(np.fft.rfft(blocks, axis=1)) ** 2, axis=2) * scale
    freqs = np.fft.rfftfreq(block, 1 / fs)
    edges = [(c / np.sqrt(2), c * np.sqrt(2)) for c in OCTAVE_CENTRES[_DRIFT_OCTAVES]]
    return np.array([spectrum[:, (freqs >= lo) & (freqs < hi)].sum(axis=1) for lo, hi in edges])


def octave_drift(
    inp: Audio, out: Audio, fs: int, latency_s: float, block_s: float = BLOCK_S
) -> Balance:
    """Balance drift and change measured on the octaves from 63 Hz to 8 kHz instead of the
    chain's own bands, so edge sets compare fairly: on its own bands, a set that merges two
    bands can no longer show them moving against each other."""
    p_in = _octave_block_powers(inp, fs, block_s)
    p_out = _octave_block_powers(_aligned(out, len(inp), latency_s, fs), fs, block_s)
    sounding = _sounding(inp, fs, block_s)[: p_in.shape[1]]
    active = sounding & np.all(_db(p_in) >= BAND_FLOOR_DBFS, axis=0)
    return _balance((_db(p_in) - _db(p_out))[:, active])


@dataclass(frozen=True)
class BandTrace:
    """One band of one input, ready for the threshold search: each counted block's power,
    spread over the detector levels it was at (rows: blocks, columns: level bins)."""

    weights: NDArray[np.float32]


def _level_bins(rms: bool) -> NDArray[np.float64]:
    """Detector levels in FFmpeg's units (power in RMS mode, amplitude in peak mode) from -100
    to +10 dB, 0.1 dB apart."""
    exponent = np.arange(-100.0, 10.0, 0.1) / (10 if rms else 20)
    return np.asarray(10**exponent, dtype=np.float64)


def calibration_traces(
    x: Audio,
    fs: int,
    edges: Sequence[float],
    settings: Sequence[Settings],
    block_s: float = BLOCK_S,
) -> list[BandTrace]:
    """Every band of one input, split as the chain splits it and run through the 3a detector
    with that band's attack, release, detection and link (the threshold does not change the
    detector, so one pass serves every threshold tried)."""
    block = round(block_s * fs)
    sounding = _sounding(x, fs, block_s)
    traces = []
    for kernel, s in zip(kernels(edges, fs), settings, strict=True):
        band = _band(x, kernel)
        level = acompressor.detector(band, fs, s.attack_ms, s.release_ms, s.rms, s.link_max)
        power = np.mean(band**2, axis=1)
        blocks = len(power) // block
        counted = sounding[:blocks] & (_db(_block_mean(power, block)) >= BAND_FLOOR_DBFS)
        bins = _level_bins(s.rms)
        which = np.clip(np.searchsorted(bins, level[: blocks * block]), 0, len(bins) - 1)
        row = np.repeat(np.arange(blocks), block)
        flat = np.bincount(row * len(bins) + which, power[: blocks * block], blocks * len(bins))
        weights = flat.reshape(blocks, len(bins))[counted] / block
        traces.append(BandTrace(weights.astype(np.float32)))
    return traces


def _gain_reduction(traces: Sequence[BandTrace], s: Settings) -> float:
    """Mean gain reduction (dB) over every counted block of every input, as compare measures it."""
    squared = (acompressor.gain(_level_bins(s.rms), s) ** 2).astype(np.float32)
    reductions = [
        _db(t.weights.sum(axis=1, dtype=np.float64)) - _db((t.weights @ squared).astype(np.float64))
        for t in traces
        if t.weights.size
    ]
    pooled = np.concatenate(reductions) if reductions else np.zeros(0)
    return float(np.mean(pooled)) if pooled.size else math.nan


def thresholds_for_gain_reduction(
    traces: Sequence[Sequence[BandTrace]], settings: Sequence[Settings], target_db: float
) -> list[float]:
    """Per band, the threshold (dB) at which its compressor, with its other settings as given,
    averages ``target_db`` of gain reduction over every input's counted blocks, as ``compare``
    measures it. ``traces`` holds each input's ``calibration_traces``. The 3a model runs on the
    Python split, so a render still confirms the result."""
    found = []
    for band, s in enumerate(settings):
        per_input = [t[band] for t in traces]
        low, high = -80.0, 0.0  # more gain reduction at the low end
        for _ in range(30):
            mid = (low + high) / 2
            trial = replace(s, threshold=10 ** (mid / 20), makeup=1.0)
            if _gain_reduction(per_input, trial) > target_db:
                low = mid
            else:
                high = mid
        found.append((low + high) / 2)
    return found
