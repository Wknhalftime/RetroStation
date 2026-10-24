"""Fits that check the chain against the brief's math: the static gain curve (item 2), attack
and release time constants (items 1 and 3), and AGC gating (item 4: no gain rise in silence).

Tone levels are peak dBFS: a sine's RMS level + 3.01 dB.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import least_squares

from processor_harness.signals import Segment

Audio = NDArray[np.float64]

_SINE_PEAK_OVER_RMS_DB = 10 * np.log10(2.0)
_SETTLED_FRACTION = 0.5  # read a static step over its second half
_EDGE_S = 0.02  # and stop short of its end, so a few ms of latency never reach the next step
_ENVELOPE_S = 0.002  # gain-trace window: two periods of the 1 kHz burst tone
_HOP_S = 0.0005
_MIN_STEP_DB = 0.5  # a smaller gain change is not a time constant to fit
_NO_BEND_DB = 0.01  # a fitted curve this close to 1:1 everywhere has no threshold in range


@dataclass(frozen=True)
class StaticFit:
    threshold_db: float
    ratio: float
    knee_db: float
    makeup_db: float
    rms_error_db: float


@dataclass(frozen=True)
class TimeConstants:
    """One-pole time constants (tau); the 10-90% time is tau x ln 9."""

    attack_s: float
    release_s: float


def _level_db(x: NDArray[np.float64]) -> float:
    return float(10 * np.log10(np.mean(x * x)) + _SINE_PEAK_OVER_RMS_DB)


def static_levels(segments: tuple[Segment, ...], out: Audio, fs: int) -> list[tuple[float, float]]:
    """(input level, settled output level) per step of the stepped tone, peak dBFS."""
    points = []
    for seg in segments:
        start = seg.start_s + _SETTLED_FRACTION * (seg.stop_s - seg.start_s)
        part = out[round(start * fs) : round((seg.stop_s - _EDGE_S) * fs), 0]
        points.append((seg.level_dbfs, _level_db(part)))
    return points


def _soft_knee(
    x: NDArray[np.float64], threshold: float, ratio: float, knee: float, makeup: float
) -> NDArray[np.float64]:
    """compress.ml's static curve (brief math item 2) plus makeup, dB in -> dB out."""
    knee = max(knee, 1e-6)
    over = x - threshold
    inside = x + (1 / ratio - 1) * (over + knee / 2) ** 2 / (2 * knee)
    y = np.where(over <= -knee / 2, x, np.where(over >= knee / 2, threshold + over / ratio, inside))
    return np.asarray(y + makeup, dtype=np.float64)


def fit_static_curve(points: list[tuple[float, float]]) -> StaticFit:
    """Least-squares threshold, ratio, knee and makeup, best of several starting thresholds."""
    x = np.array([p[0] for p in points])
    y = np.array([p[1] for p in points])

    def residual(v: NDArray[np.float64]) -> NDArray[np.float64]:
        return np.asarray(_soft_knee(x, v[0], v[1], v[2], v[3]) - y, dtype=np.float64)

    best = None
    for start in np.arange(x.min(), x.max() + 1e-9, 5.0):
        fit = least_squares(
            residual, [start, 2.0, 2.0, 0.0], bounds=([-90, 1, 0, -40], [10, 100, 40, 40])
        )
        if best is None or fit.cost < best.cost:
            best = fit
    assert best is not None
    t, r, w, m = (float(v) for v in best.x)
    error = float(np.sqrt(np.mean(residual(best.x) ** 2)))
    if np.max(np.abs(_soft_knee(x, t, r, w, m) - (x + m))) < _NO_BEND_DB:
        # The curve never bends inside the measured range: no threshold to report, and any
        # ratio fits equally well, so the chain reads as 1:1.
        return StaticFit(math.nan, 1.0, math.nan, m, error)
    return StaticFit(t, r, w, m, error)


def _envelope_db(x: NDArray[np.float64], fs: int) -> NDArray[np.float64]:
    window, hop = round(_ENVELOPE_S * fs), round(_HOP_S * fs)
    running = np.concatenate([[0.0], np.cumsum(x * x)])
    starts = np.arange(0, len(x) - window + 1, hop)
    with np.errstate(divide="ignore"):
        return np.asarray(10 * np.log10((running[starts + window] - running[starts]) / window))


def _fit_tau(t: NDArray[np.float64], gain: NDArray[np.float64]) -> float:
    """tau of gain(t) = end + (start - end) exp(-t / tau), or nan if the gain barely moves."""
    if abs(gain[0] - gain[-1]) < _MIN_STEP_DB:
        return float("nan")

    def residual(v: NDArray[np.float64]) -> NDArray[np.float64]:
        return np.asarray(v[0] + (v[1] - v[0]) * np.exp(-t / v[2]) - gain, dtype=np.float64)

    fit = least_squares(
        residual, [gain[-1], gain[0], 0.05], bounds=([-np.inf, -np.inf, 1e-5], [np.inf] * 3)
    )
    return float(fit.x[2])


def time_constants(
    segments: tuple[Segment, ...], inp: Audio, out: Audio, fs: int, latency_s: float
) -> TimeConstants:
    """Median attack (level up) and release (level down) tau over the tone bursts' steps.

    The gain trace is output over input level in 2 ms windows every 0.5 ms, with the output
    moved back by the chain's latency (measured on the impulse).
    """
    shift = round(latency_s * fs)
    aligned = np.zeros(len(inp))
    tail = out[shift : shift + len(inp), 0]
    aligned[: len(tail)] = tail
    gain = _envelope_db(aligned, fs) - _envelope_db(inp[:, 0], fs)
    hop, window = round(_HOP_S * fs) / fs, round(_ENVELOPE_S * fs) / fs
    attacks: list[float] = []
    releases: list[float] = []
    for before, seg in zip(segments, segments[1:], strict=False):
        first = int(np.ceil((seg.start_s + window) / hop))
        last = int((seg.stop_s - window) / hop)
        trace = gain[first:last]
        t = np.arange(len(trace), dtype=np.float64) * hop
        # Windows past the end of a shorter (latency-shifted) output have no level.
        finite = np.isfinite(trace)
        tau = _fit_tau(t[finite], trace[finite])
        (attacks if seg.level_dbfs > before.level_dbfs else releases).append(tau)
    return TimeConstants(_median(attacks), _median(releases))


def _median(values: list[float]) -> float:
    """Median of the fitted values; nan when no step moved the gain (an idle chain)."""
    fitted = [v for v in values if not math.isnan(v)]
    return float(np.median(fitted)) if fitted else math.nan


def pause_rise(
    times: NDArray[np.float64], gain: NDArray[np.float64], pauses: tuple[Segment, ...]
) -> float:
    """Largest climb of a logged gain (in its own units: dB for the AGCs) within any pause,
    measured from its value as the pause starts; falling gain counts as zero."""
    rise = 0.0
    for pause in pauses:
        inside = gain[(times >= pause.start_s) & (times < pause.stop_s)]
        if inside.size:
            rise = max(rise, float(np.max(inside) - inside[0]))
    return rise
