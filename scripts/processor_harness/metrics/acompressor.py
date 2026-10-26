"""FFmpeg's acompressor, sample for sample (brief math item 3a): libavfilter/af_sidechaincompress.c
and hermite.h. The DSP is the same at n7.1 (the FFmpeg Liquidsoap links) and on master: the two
differ only in API boilerplate (diffed 2026-10-24). Downward mode, mix 1, level_in 1.

- Detection smooths the level, not the gain: the channels' |x| are averaged (link "average",
  the default) or maximised ("maximum"), squared in RMS mode (the default), then go through a
  one-pole with c = min(1, 4000 / (t_ms fs)): attack while the input is above the smoothed
  level, release otherwise. So the time constant is a quarter of the set value.
- The gain follows the smoothed level every sample, with no further smoothing: unity until the
  level passes the knee's start, then the ratio in natural-log units, with a Hermite knee from
  threshold / sqrt(k) to threshold * sqrt(k) (20 log10 k dB wide).
- Threshold, knee and makeup are linear values, as FFmpeg takes them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

Audio = NDArray[np.float64]

_FAKE_INFINITY = 65536.0 * 65536.0


@dataclass(frozen=True)
class Settings:
    threshold: float  # linear
    ratio: float
    knee: float  # linear ratio, >= 1
    makeup: float  # linear
    attack_ms: float
    release_ms: float
    rms: bool = True  # detection: rms (FFmpeg's default) or peak
    link_max: bool = False  # link: average (FFmpeg's default) or maximum

    @classmethod
    def from_db(
        cls,
        threshold_db: float,
        ratio: float,
        knee_db: float,
        makeup_db: float,
        attack_ms: float,
        release_ms: float,
        rms: bool = True,
        link_max: bool = False,
    ) -> Settings:
        """Settings from dB values; the knee is given by its width in dB."""
        return cls(
            10 ** (threshold_db / 20),
            ratio,
            10 ** (knee_db / 20),
            10 ** (makeup_db / 20),
            attack_ms,
            release_ms,
            rms,
            link_max,
        )


def _coefficient(t_ms: float, fs: int) -> float:
    return min(1.0, 4000.0 / (t_ms * fs))


def detector(
    x: Audio, fs: int, attack_ms: float, release_ms: float, rms: bool, link_max: bool
) -> NDArray[np.float64]:
    """The smoothed detection level per sample (``lin_slope`` in the C code)."""
    magnitude = np.abs(x)
    d = np.max(magnitude, axis=1) if link_max else np.mean(magnitude, axis=1)
    if rms:
        d = d * d
    attack, release = _coefficient(attack_ms, fs), _coefficient(release_ms, fs)
    out = np.empty(len(d))
    level = 0.0
    # The one-pole switches coefficient on its own state, so it cannot be vectorised.
    for i, v in enumerate(d.tolist()):
        level += (v - level) * (attack if v > level else release)
        out[i] = level
    return out


def _hermite(
    x: NDArray[np.float64], x0: float, x1: float, p0: float, p1: float, m0: float, m1: float
) -> NDArray[np.float64]:
    width = x1 - x0
    t = (x - x0) / width
    m0, m1 = m0 * width, m1 * width
    c2 = -3 * p0 - 2 * m0 + 3 * p1 - m1
    c3 = 2 * p0 + m0 - 2 * p1 + m1
    return np.asarray(c3 * t**3 + c2 * t**2 + m0 * t + p0, dtype=np.float64)


def gain(level: NDArray[np.float64], s: Settings) -> NDArray[np.float64]:
    """Linear gain for each smoothed detection level (``output_gain``, before makeup)."""
    thres = math.log(s.threshold)
    knee_start = math.log(s.threshold / math.sqrt(s.knee))
    knee_stop = math.log(s.threshold * math.sqrt(s.knee))
    compressed_knee_stop = (knee_stop - thres) / s.ratio + thres
    onset = (s.threshold / math.sqrt(s.knee)) ** (2 if s.rms else 1)
    active = level > max(onset, 0.0)
    slope = np.log(np.where(active, level, 1.0))
    if s.rms:
        slope *= 0.5
    if abs(s.ratio - _FAKE_INFINITY) < 1.0:
        out, delta = np.full_like(slope, thres), 0.0
    else:
        out, delta = (slope - thres) / s.ratio + thres, 1.0 / s.ratio
    if s.knee > 1.0:
        inside = slope < knee_stop
        knee = _hermite(slope, knee_start, knee_stop, knee_start, compressed_knee_stop, 1.0, delta)
        out = np.where(inside, knee, out)
    return np.where(active, np.exp(out - slope), 1.0)


def process(x: Audio, fs: int, s: Settings) -> Audio:
    """The filter's output for input x (stereo-linked gain on every channel, then makeup)."""
    level = detector(x, fs, s.attack_ms, s.release_ms, s.rms, s.link_max)
    return np.asarray(x * (gain(level, s) * s.makeup)[:, np.newaxis], dtype=np.float64)
