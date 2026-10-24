"""Which analysis each input kind gets, from the brief's test-signal table. A measurement sees
the input and output audio, the stimulus schedule, the gauges the chain logged, and for
programme the decoded codec round trip.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from processor_harness.metrics import distortion, dynamics, fits, loudness, response, transients
from processor_harness.signals import SILENT, Segment

Audio = NDArray[np.float64]
Trace = tuple[NDArray[np.float64], NDArray[np.float64]]

PROGRAMME = "programme"


@dataclass(frozen=True, eq=False)
class Rendered:
    inp: Audio
    out: Audio
    fs: int
    segments: tuple[Segment, ...]
    gauges: Mapping[str, Trace]
    decoded: Audio | None
    ceiling_dbtp: float
    latency_s: float
    category: str = ""  # programme category; "" for stimuli


def read_gauges(csv: Path) -> dict[str, Trace]:
    """The gauges CSV a Liquidsoap render writes, as (times, values) per gauge name."""
    rows: dict[str, tuple[list[float], list[float]]] = {}
    for line in csv.read_text(encoding="utf-8").splitlines()[1:]:
        t, name, value = line.split(",")
        times, values = rows.setdefault(name, ([], []))
        times.append(float(t))
        values.append(float(value))
    return {name: (np.array(t), np.array(v)) for name, (t, v) in rows.items()}


def _impulse(r: Rendered) -> dict[str, float]:
    low, high = response.band_sum_range(r.inp, r.out, r.fs)
    latency_s = response.latency(r.inp, r.out, r.fs)
    row = {
        "band_sum_min_db": low,
        "band_sum_max_db": high,
        "latency_ms": 1000 * latency_s,
    }
    # The click's pre-ring lines up by the latency just measured on it.
    row |= _pre_ring(r, latency_s)
    # The shape of the response (the band's shape, for a preset that renders one band solo).
    _, levels = response.third_octave_response(r.inp, r.out, r.fs)
    for label, level in zip(response.THIRD_OCTAVE_LABELS, levels, strict=True):
        row[f"response_{label}hz_db"] = float(level)
    return row


def _pre_ring(r: Rendered, latency_s: float | None = None) -> dict[str, float]:
    """Pre-ringing around the input's transient, lined up by the chain's latency."""
    lag = r.latency_s if latency_s is None else latency_s
    ring = transients.pre_ring(r.inp, r.out, r.fs, lag)
    return {
        "pre_ring_energy_db": ring.energy_db,
        "pre_ring_reach_ms": ring.reach_ms,
        "pre_ring_beyond_20ms_db": ring.beyond_20ms_db,
    }


def _sweep(r: Rendered) -> dict[str, float]:
    return {"response_deviation_db": response.sweep_deviation(r.inp, r.out, r.fs)}


def _stepped_tone(r: Rendered) -> dict[str, float]:
    fit = fits.fit_static_curve(fits.static_levels(r.segments, r.out, r.fs))
    return {
        "static_threshold_db": fit.threshold_db,
        "static_ratio": fit.ratio,
        "static_knee_db": fit.knee_db,
        "static_makeup_db": fit.makeup_db,
        "static_fit_error_db": fit.rms_error_db,
    }


def _tone_bursts(r: Rendered) -> dict[str, float]:
    tc = fits.time_constants(r.segments, r.inp, r.out, r.fs, r.latency_s)
    return {"attack_tau_ms": 1000 * tc.attack_s, "release_tau_ms": 1000 * tc.release_s}


def _tone_frequency(r: Rendered) -> float:
    """The tone's frequency, from the input's spectral peak."""
    spectrum = np.abs(np.fft.rfft(r.inp[:, 0]))
    return float(np.fft.rfftfreq(len(r.inp), 1 / r.fs)[np.argmax(spectrum)])


def _tone(r: Rendered) -> dict[str, float]:
    return {"thd_pct": 100 * distortion.thd(r.out, r.fs, _tone_frequency(r))}


def _smpte(r: Rendered) -> dict[str, float]:
    return {"imd_smpte_pct": 100 * distortion.imd_smpte(r.out, r.fs)}


def _ccif(r: Rendered) -> dict[str, float]:
    return {"imd_ccif_pct": 100 * distortion.imd_ccif(r.out, r.fs)}


def _pauses(r: Rendered) -> dict[str, float]:
    pauses = tuple(s for s in r.segments if s.level_dbfs == SILENT)
    return {
        f"pause_rise.{name}_db": fits.pause_rise(times, values, pauses)
        for name, (times, values) in r.gauges.items()
    }


def _percentile(values: NDArray[np.float64], q: float) -> float:
    return float(np.percentile(values, q)) if values.size else math.nan


def _programme(r: Rendered) -> dict[str, float]:
    psr = dynamics.psr(r.out, r.fs)
    lufs = loudness.integrated(r.out, r.fs)
    peak = loudness.true_peak(r.out, r.fs)
    row = {
        "lufs_i": lufs,
        "loudness_change_lu": lufs - loudness.integrated(r.inp, r.fs),
        "lra_lu": loudness.loudness_range(r.out, r.fs),
        "true_peak_dbtp": peak,
        "true_peak_rise_db": peak - loudness.true_peak(r.inp, r.fs),
        "sample_peak_dbfs": loudness.to_db(float(np.max(np.abs(r.out)))),
        "plr_lu": dynamics.plr(r.out, r.fs),
        "psr_p10_lu": _percentile(psr, 10),
        "psr_p50_lu": _percentile(psr, 50),
        "psr_p90_lu": _percentile(psr, 90),
        "crest_db": dynamics.crest_factor(r.out),
        "sample_overs": float(dynamics.sample_overs(r.out, r.ceiling_dbtp)),
        "true_overs": float(dynamics.true_overs(r.out, r.fs, r.ceiling_dbtp)),
    }
    if r.category == "transient":
        row |= _pre_ring(r)
    if r.decoded is not None:
        row["codec_true_peak_dbtp"] = loudness.true_peak(r.decoded, r.fs)
        row["codec_true_overs"] = float(dynamics.true_overs(r.decoded, r.fs, r.ceiling_dbtp))
    return row


MEASURES: dict[str, Callable[[Rendered], dict[str, float]]] = {
    "impulse": _impulse,
    "log_sweep": _sweep,
    "stepped_tone": _stepped_tone,
    "tone_bursts": _tone_bursts,
    "tone": _tone,
    "smpte_imd": _smpte,
    "ccif_imd": _ccif,
    "noise_with_pauses": _pauses,
    "kick": lambda r: _pre_ring(r),
    "snare": lambda r: _pre_ring(r),
    PROGRAMME: _programme,
}


def measure(kind: str, rendered: Rendered) -> dict[str, float]:
    """The metrics for one render of an input of this kind."""
    if kind not in MEASURES:
        raise KeyError(f"no measurement for input kind {kind!r}")
    return MEASURES[kind](rendered)
