"""ITU-R BS.1770-5 loudness, EBU Tech 3342 loudness range and true peak, at any sample rate.

K-weighting is designed from the BS.1770 analog prototype (the parameters libebur128 uses), so
it holds at 44.1 kHz, the stream's rate; at 48 kHz it reproduces the standard's coefficient
table. Every channel weighs 1.0 (stereo: no surround channels here).
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
from numpy.typing import NDArray
from scipy import signal

Audio = NDArray[np.float64]

ABSOLUTE_GATE_LUFS = -70.0
RELATIVE_GATE_LU = -10.0
LRA_RELATIVE_GATE_LU = -20.0
STEP_S = 0.1
MOMENTARY_S = 0.4
SHORT_TERM_S = 3.0
OVERSAMPLE = 4

_SHELF_F0 = 1681.974450955533
_SHELF_GAIN_DB = 3.999843853973347
_SHELF_Q = 0.7071752369554196
_SHELF_VB_EXPONENT = 0.4996667741545416
_HIGHPASS_F0 = 38.13547087602444
_HIGHPASS_Q = 0.5003270373238773
# Input samples of context either side of a resampling chunk: more than resample_poly's filter
# half-length (10 input samples at 4x), so chunk edges match a whole-file resample.
_RESAMPLE_PAD = 64


def k_weighting(fs: int) -> NDArray[np.float64]:
    """The two K-weighting biquads (head shelf, then RLB high-pass) as scipy SOS rows."""
    k = np.tan(np.pi * _SHELF_F0 / fs)
    vh = 10 ** (_SHELF_GAIN_DB / 20)
    vb = vh**_SHELF_VB_EXPONENT
    a0 = 1 + k / _SHELF_Q + k * k
    shelf = [
        (vh + vb * k / _SHELF_Q + k * k) / a0,
        2 * (k * k - vh) / a0,
        (vh - vb * k / _SHELF_Q + k * k) / a0,
        1.0,
        2 * (k * k - 1) / a0,
        (1 - k / _SHELF_Q + k * k) / a0,
    ]
    k = np.tan(np.pi * _HIGHPASS_F0 / fs)
    a0 = 1 + k / _HIGHPASS_Q + k * k
    highpass = [1.0, -2.0, 1.0, 1.0, 2 * (k * k - 1) / a0, (1 - k / _HIGHPASS_Q + k * k) / a0]
    return np.array([shelf, highpass])


def to_lufs(power: NDArray[np.float64] | float) -> NDArray[np.float64]:
    """Channel-summed K-weighted mean square to LUFS; zero power is -inf."""
    with np.errstate(divide="ignore"):
        return np.asarray(-0.691 + 10 * np.log10(power), dtype=np.float64)


def block_powers(x: Audio, fs: int, window_s: float) -> NDArray[np.float64]:
    """K-weighted mean square summed over channels, per window_s window stepped 100 ms."""
    y = signal.sosfilt(k_weighting(fs), x, axis=0)
    squares = np.sum(y * y, axis=1)
    window = round(window_s * fs)
    step = round(STEP_S * fs)
    if len(squares) < window:
        return np.empty(0)
    running = np.concatenate([[0.0], np.cumsum(squares)])
    starts = np.arange(0, len(squares) - window + 1, step)
    return np.asarray((running[starts + window] - running[starts]) / window, dtype=np.float64)


def _gated_mean(powers: NDArray[np.float64], relative_lu: float) -> NDArray[np.float64]:
    """The blocks that pass the -70 LUFS absolute gate and then the relative gate."""
    powers = powers[to_lufs(powers) > ABSOLUTE_GATE_LUFS]
    if powers.size == 0:
        return powers
    threshold = to_lufs(float(np.mean(powers))) + relative_lu
    return powers[to_lufs(powers) > threshold]


def integrated(x: Audio, fs: int) -> float:
    """Gated integrated loudness (LUFS) over 400 ms blocks with 75% overlap."""
    gated = _gated_mean(block_powers(x, fs, MOMENTARY_S), RELATIVE_GATE_LU)
    if gated.size == 0:
        return float("-inf")
    return float(to_lufs(float(np.mean(gated))))


def momentary(x: Audio, fs: int) -> NDArray[np.float64]:
    """Momentary loudness (400 ms windows) every 100 ms, LUFS."""
    return to_lufs(block_powers(x, fs, MOMENTARY_S))


def short_term(x: Audio, fs: int) -> NDArray[np.float64]:
    """Short-term loudness (3 s windows) every 100 ms, LUFS."""
    return to_lufs(block_powers(x, fs, SHORT_TERM_S))


def loudness_range(x: Audio, fs: int) -> float:
    """EBU Tech 3342 LRA: 95th minus 10th percentile of gated short-term loudness, LU."""
    gated = _gated_mean(block_powers(x, fs, SHORT_TERM_S), LRA_RELATIVE_GATE_LU)
    if gated.size == 0:
        return 0.0
    values = to_lufs(gated)
    return float(np.percentile(values, 95) - np.percentile(values, 10))


def oversampled(x: Audio, fs: int) -> Iterator[tuple[int, NDArray[np.float64]]]:
    """|x| at 4x the rate, one chunk at a time: (first input sample, magnitudes) per chunk
    per channel. Chunks are whole 100 ms blocks, resampled with padding either side so the
    edges match a whole-file resample, and a long programme file never sits in memory 4x over.
    The original samples are folded in, so no peak is lost to the resampler's ripple.
    """
    chunk = round(STEP_S * fs) * 60
    for channel in x.T:
        for start in range(0, len(channel), chunk):
            stop = min(len(channel), start + chunk)
            lo = max(0, start - _RESAMPLE_PAD)
            hi = min(len(channel), stop + _RESAMPLE_PAD)
            up = np.abs(signal.resample_poly(channel[lo:hi], OVERSAMPLE, 1))
            up = up[(start - lo) * OVERSAMPLE : (stop - lo) * OVERSAMPLE]
            # Every 4th output sample sits on an input sample.
            up[::OVERSAMPLE] = np.maximum(up[::OVERSAMPLE], np.abs(channel[start:stop]))
            yield start, up


def true_peak_blocks(x: Audio, fs: int) -> NDArray[np.float64]:
    """Largest 4x-oversampled |sample| per 100 ms block, linear, over all channels."""
    block = round(STEP_S * fs)
    peaks = np.zeros(-(-len(x) // block))
    for start, up in oversampled(x, fs):
        per_block = block * OVERSAMPLE
        padded = np.pad(up, (0, -len(up) % per_block))
        maxima = padded.reshape(-1, per_block).max(axis=1)
        first = start // block
        peaks[first : first + len(maxima)] = np.maximum(peaks[first : first + len(maxima)], maxima)
    return peaks


def to_db(linear: float) -> float:
    """Linear amplitude to dB; zero is -inf."""
    return float("-inf") if linear <= 0 else float(20 * np.log10(linear))


def true_peak(x: Audio, fs: int) -> float:
    """Maximum true peak over the whole signal, dBTP."""
    peaks = true_peak_blocks(x, fs)
    return to_db(float(np.max(peaks))) if peaks.size else float("-inf")
