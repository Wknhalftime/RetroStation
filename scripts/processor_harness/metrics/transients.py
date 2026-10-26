"""Pre-ringing around a transient. A linear-phase FIR centres its response on its delay, so with
bands at unequal gains (what compression does) part of a hit can appear up to one latency before
the hit itself. Backward masking covers only about 20 ms, so what matters is how much arrives
earlier than that.

Measured on the output lined up with the input by the chain's latency, against the input's
onset (its first sample within 30 dB of its peak). The envelope is a 1 ms trailing RMS, so the
hit itself never leaks into the pre-onset region. All levels are relative to the output's
envelope peak in the 50 ms after the onset:
- energy_db: all energy before the onset over the energy of those 50 ms;
- reach_ms: how far before the onset the envelope first comes within 60 dB of the peak;
- beyond_20ms_db: the envelope's highest level more than 20 ms before the onset (the proposed
  pass mark is below -40 dB).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

Audio = NDArray[np.float64]

ONSET_DB = -30.0
REACH_DB = -60.0
MASKED_S = 0.020
POST_ONSET_S = 0.050
_ENVELOPE_S = 0.001


@dataclass(frozen=True)
class PreRing:
    energy_db: float
    reach_ms: float
    beyond_20ms_db: float


def onset(x: Audio) -> int:
    """Where the transient's attack starts: from the first sample (any channel) within 30 dB of
    the peak, back to just after the last sample more than 60 dB under it, so the first quiet
    samples of the attack count as the hit rather than as pre-ringing."""
    magnitude = np.max(np.abs(x), axis=1)
    peak = np.max(magnitude)
    crossing = int(np.argmax(magnitude >= peak * 10 ** (ONSET_DB / 20)))
    quiet = np.flatnonzero(magnitude[:crossing] < peak * 10 ** (REACH_DB / 20))
    return int(quiet[-1] + 1) if quiet.size else 0


def _aligned(out: Audio, length: int, shift: int) -> Audio:
    y = np.zeros((length, out.shape[1]))
    tail = out[shift : shift + length]
    y[: len(tail)] = tail
    return y


def pre_ring(inp: Audio, out: Audio, fs: int, latency_s: float) -> PreRing:
    """Pre-ringing of the output around the input's transient."""
    start = onset(inp)
    y = _aligned(out, len(inp), round(latency_s * fs))
    power = np.mean(y * y, axis=1)
    window = round(_ENVELOPE_S * fs)
    running = np.concatenate([np.zeros(window), np.cumsum(power)])
    envelope = np.sqrt((running[window:] - running[:-window]) / window)
    after = slice(start, start + round(POST_ONSET_S * fs))
    with np.errstate(divide="ignore"):
        relative = 20 * np.log10(envelope / np.max(envelope[after]))
        energy = float(10 * np.log10(np.sum(power[:start]) / np.sum(power[after])))
    close = np.flatnonzero(relative[:start] >= REACH_DB)
    reach = (start - close[0]) / fs * 1000 if close.size else 0.0
    early_end = start - round(MASKED_S * fs)
    beyond = float(np.max(relative[:early_end])) if early_end > 0 else float("-inf")
    return PreRing(energy, float(reach), beyond)
