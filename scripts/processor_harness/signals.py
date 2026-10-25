"""Synthetic stimuli from the brief's test-signal table. Each is stereo with identical channels
and carries its schedule (segments with their levels), which is what its analysis needs.

Tone levels are peak dBFS (as in the EBU test cases); noise levels are RMS dBFS. A pause has
level -inf.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

Audio = NDArray[np.float64]

SILENT = float("-inf")
_FADE_S = 0.01


@dataclass(frozen=True)
class Segment:
    start_s: float
    stop_s: float
    level_dbfs: float
    label: str = ""  # a sequence item's programme id


@dataclass(frozen=True, eq=False)
class Stimulus:
    samples: Audio
    fs: int
    segments: tuple[Segment, ...]


def _stereo(mono: NDArray[np.float64]) -> Audio:
    return np.column_stack([mono, mono])


def _lin(level_dbfs: float) -> float:
    return float(10 ** (level_dbfs / 20))


def _held_levels(levels: list[float], hold_s: float) -> tuple[Segment, ...]:
    return tuple(Segment(i * hold_s, (i + 1) * hold_s, level) for i, level in enumerate(levels))


def _tone_at_levels(fs: int, freq: float, segments: tuple[Segment, ...]) -> Stimulus:
    """One continuous-phase sine whose amplitude follows the segments (no phase jumps)."""
    n = round(segments[-1].stop_s * fs)
    envelope = np.zeros(n)
    for seg in segments:
        envelope[round(seg.start_s * fs) : round(seg.stop_s * fs)] = _lin(seg.level_dbfs)
    mono = envelope * np.sin(2 * np.pi * freq * np.arange(n) / fs)
    return Stimulus(_stereo(mono), fs, segments)


def tone(fs: int, freq: float, level_dbfs: float, seconds: float) -> Stimulus:
    """Steady sine (THD)."""
    return _tone_at_levels(fs, freq, (Segment(0.0, seconds, level_dbfs),))


def impulse(fs: int, level_dbfs: float, seconds: float, at_s: float) -> Stimulus:
    """One sample at level_dbfs (band-sum flatness, latency)."""
    mono = np.zeros(round(seconds * fs))
    mono[round(at_s * fs)] = _lin(level_dbfs)
    return Stimulus(_stereo(mono), fs, (Segment(at_s, at_s, level_dbfs),))


def log_sweep(fs: int, f1: float, f2: float, level_dbfs: float, seconds: float) -> Stimulus:
    """Exponential sine sweep f1 -> f2 (frequency response), 10 ms fades at both ends."""
    t = np.arange(round(seconds * fs)) / fs
    rate = np.log(f2 / f1)
    phase = 2 * np.pi * f1 * seconds / rate * (np.exp(t * rate / seconds) - 1)
    mono = _lin(level_dbfs) * np.sin(phase)
    fade = round(_FADE_S * fs)
    ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(fade) / fade)
    mono[:fade] *= ramp
    mono[-fade:] *= ramp[::-1]
    return Stimulus(_stereo(mono), fs, (Segment(0.0, seconds, level_dbfs),))


def stepped_tone(
    fs: int,
    freq: float,
    start_dbfs: float,
    stop_dbfs: float,
    hold_s: float,
    step_db: float = 2.0,
) -> Stimulus:
    """Tone held at each level from start to stop (static gain curve, math item 2)."""
    count = round((stop_dbfs - start_dbfs) / step_db) + 1
    levels = [start_dbfs + i * step_db for i in range(count)]
    return _tone_at_levels(fs, freq, _held_levels(levels, hold_s))


def tone_bursts(
    fs: int, freq: float, base_dbfs: float, step_db: float, hold_s: float, cycles: int
) -> Stimulus:
    """Tone alternating base and base + step_db, ending on base (attack and release times)."""
    levels = [base_dbfs, base_dbfs + step_db] * cycles + [base_dbfs]
    return _tone_at_levels(fs, freq, _held_levels(levels, hold_s))


def twin_tone(
    fs: int, f1: float, f2: float, ratio: float, level_dbfs: float, seconds: float
) -> Stimulus:
    """f1 at ratio x the amplitude of f2, scaled so the sum peaks at level_dbfs (IMD).

    SMPTE: 60 Hz + 7 kHz at 4:1. CCIF: 19 kHz + 20 kHz at 1:1.
    """
    t = np.arange(round(seconds * fs)) / fs
    mono = ratio * np.sin(2 * np.pi * f1 * t) + np.sin(2 * np.pi * f2 * t)
    mono *= _lin(level_dbfs) / np.max(np.abs(mono))
    return Stimulus(_stereo(mono), fs, (Segment(0.0, seconds, level_dbfs),))


def smpte_twin_tone(fs: int, level_dbfs: float, seconds: float) -> Stimulus:
    """SMPTE IMD stimulus: 60 Hz + 7 kHz at 4:1."""
    return twin_tone(fs, f1=60.0, f2=7000.0, ratio=4.0, level_dbfs=level_dbfs, seconds=seconds)


def ccif_twin_tone(fs: int, level_dbfs: float, seconds: float) -> Stimulus:
    """CCIF IMD stimulus: 19 kHz + 20 kHz at 1:1."""
    return twin_tone(fs, f1=19000.0, f2=20000.0, ratio=1.0, level_dbfs=level_dbfs, seconds=seconds)


def _hit(
    fs: int, level_dbfs: float, seconds: float, at_s: float, hit: NDArray[np.float64]
) -> Stimulus:
    """A drum hit placed at at_s after digital silence, scaled to peak at level_dbfs."""
    mono = np.zeros(round(seconds * fs))
    start = round(at_s * fs)
    mono[start : start + len(hit)] = hit[: len(mono) - start]
    mono *= _lin(level_dbfs) / np.max(np.abs(mono))
    return Stimulus(_stereo(mono), fs, (Segment(at_s, seconds, level_dbfs),))


def kick(fs: int, level_dbfs: float, seconds: float, at_s: float) -> Stimulus:
    """Synthetic kick drum from silence (pre-ringing): a sine gliding from 150 Hz down to 50 Hz
    within about 30 ms, decaying with a 150 ms time constant."""
    t = np.arange(round((seconds - at_s) * fs)) / fs
    freq = 50.0 + 100.0 * np.exp(-t / 0.03)
    hit = np.sin(2 * np.pi * np.cumsum(freq) / fs) * np.exp(-t / 0.15)
    return _hit(fs, level_dbfs, seconds, at_s, hit)


def snare(fs: int, level_dbfs: float, seconds: float, at_s: float, seed: int = 0) -> Stimulus:
    """Synthetic snare drum from silence (pre-ringing): a 190 Hz body (80 ms decay) under
    white-noise wires (120 ms decay)."""
    t = np.arange(round((seconds - at_s) * fs)) / fs
    wires = np.random.default_rng(seed).standard_normal(len(t))
    hit = 0.5 * np.sin(2 * np.pi * 190.0 * t) * np.exp(-t / 0.08)
    hit += 0.5 * wires * np.exp(-t / 0.12)
    return _hit(fs, level_dbfs, seconds, at_s, hit)


def _pink(n: int, rng: np.random.Generator) -> NDArray[np.float64]:
    spectrum = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n)
    spectrum[0] = 0.0
    spectrum[1:] /= np.sqrt(f[1:])
    return np.asarray(np.fft.irfft(spectrum, n), dtype=np.float64)


def noise_with_pauses(
    fs: int, burst_s: float, pause_s: float, bursts: int, level_dbfs: float, seed: int = 0
) -> Stimulus:
    """Pink-noise bursts at level_dbfs RMS, each followed by digital silence (AGC gating)."""
    rng = np.random.default_rng(seed)
    parts: list[NDArray[np.float64]] = []
    segments: list[Segment] = []
    t = 0.0
    for _ in range(bursts):
        burst = _pink(round(burst_s * fs), rng)
        burst *= _lin(level_dbfs) / np.sqrt(np.mean(burst * burst))
        parts += [burst, np.zeros(round(pause_s * fs))]
        segments += [
            Segment(t, t + burst_s, level_dbfs),
            Segment(t + burst_s, t + burst_s + pause_s, SILENT),
        ]
        t += burst_s + pause_s
    return Stimulus(_stereo(np.concatenate(parts)), fs, tuple(segments))
