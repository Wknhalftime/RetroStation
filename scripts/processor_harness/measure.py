"""Which analysis each input kind gets, from the brief's test-signal table. A measurement sees
the input and output audio, the stimulus schedule, the gauges the chain logged, and for
programme the decoded codec round trip.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from processor_harness.metrics import (
    bands,
    distortion,
    dynamics,
    fits,
    loudness,
    response,
    transients,
)
from processor_harness.metrics.acompressor import Settings
from processor_harness.signals import SILENT, Segment

Audio = NDArray[np.float64]
Trace = tuple[NDArray[np.float64], NDArray[np.float64]]

PROGRAMME = "programme"
# A float as Liquidsoap prints one (or nan / inf).
_NUMBER = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$|^-?inf$|^nan$")
SEQUENCE = "sequence"
PINNED_WITHIN_DB = 1.0  # a gauge this close to a declared limit counts as pinned


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
    model: Settings | None = None  # the chain's acompressor settings, when the preset says
    gauge_limits: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    level_check: tuple[str, float] | None = None  # (gauge, target LUFS) for playlists
    band_edges: tuple[float, ...] = ()  # the chain's crossover, for per-band measures
    band_makeup_db: tuple[float, ...] = ()  # each band's makeup, left out of its reduction


def read_gauges(csv: Path) -> dict[str, Trace]:
    """The gauges CSV a Liquidsoap render writes, as (times, values) per gauge name."""
    rows: dict[str, tuple[list[float], list[float]]] = {}
    for line in csv.read_text(encoding="utf-8").splitlines()[1:]:
        fields = line.split(",")
        # Liquidsoap's log shares stdout with the gauge lines and can split one: skip pieces.
        if len(fields) != 3 or not (_NUMBER.match(fields[0]) and _NUMBER.match(fields[2])):
            continue
        t, name, value = fields
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
    if r.model is not None:
        fit = fits.fit_acompressor_static(r.segments, r.inp, r.out, r.fs, r.model, r.latency_s)
    else:
        fit = fits.fit_static_curve(fits.static_levels(r.segments, r.out, r.fs))
    return {
        "static_threshold_db": fit.threshold_db,
        "static_ratio": fit.ratio,
        "static_knee_db": fit.knee_db,
        "static_makeup_db": fit.makeup_db,
        "static_fit_error_db": fit.rms_error_db,
    }


def _tone_bursts(r: Rendered) -> dict[str, float]:
    if r.model is not None:
        times = fits.fit_acompressor_times(r.segments, r.inp, r.out, r.fs, r.model, r.latency_s)
        return {
            "attack_ms": times.attack_ms,
            "release_ms": times.release_ms,
            "attack_tau_ms": times.attack_ms / 4,
            "release_tau_ms": times.release_ms / 4,
            "times_fit_error_db": times.rms_error_db,
        }
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


def _gauge_movement(
    name: str, times: NDArray[np.float64], values: NDArray[np.float64]
) -> dict[str, float]:
    """How much a gauge moves (for the AGC: pumping versus correcting a level error): total
    travel per minute, and the 95th percentile of how far it moves in any one second."""
    if values.size < 2:
        return {}
    step = float(np.median(np.diff(times)))
    minutes = (times[-1] - times[0] + step) / 60
    lag = max(1, round(1.0 / step))
    swing = np.abs(values[lag:] - values[:-lag]) if values.size > lag else np.zeros(1)
    return {
        f"gauge.{name}.travel_db_per_min": float(np.sum(np.abs(np.diff(values))) / minutes),
        f"gauge.{name}.swing_1s_p95_db": float(np.percentile(swing, 95)),
    }


def _gauge_spread(r: Rendered) -> dict[str, float]:
    """Where each logged gauge spent the render: extremes and 5th / 50th / 95th percentile
    (for the AGC, how close its gain runs to its range limits)."""
    row: dict[str, float] = {}
    for name, (times, values) in r.gauges.items():
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            continue
        row |= _gauge_movement(name, times[np.isfinite(values)], finite)
        row[f"gauge.{name}.min"] = float(np.min(finite))
        row[f"gauge.{name}.max"] = float(np.max(finite))
        for q in (5, 50, 95):
            row[f"gauge.{name}.p{q:02d}"] = float(np.percentile(finite, q))
        if name in r.gauge_limits:
            low, high = r.gauge_limits[name]
            pinned = (finite <= low + PINNED_WITHIN_DB) | (finite >= high - PINNED_WITHIN_DB)
            row[f"gauge.{name}.pinned_pct"] = 100 * float(np.mean(pinned))
    return row


def _bands(r: Rendered) -> dict[str, float]:
    """Each band's input and output level, its gain reduction (mean and 95th percentile),
    and how the balance between bands moved (metrics.bands)."""
    c = bands.compare(r.inp, r.out, r.fs, r.band_edges, r.latency_s)
    grid = bands.octave_drift(r.inp, r.out, r.fs, r.latency_s)
    row = {
        "balance_drift_db": c.drift_db,
        "balance_change_db": c.change_db,
        "octave_drift_db": grid.drift_db,
        "octave_change_db": grid.change_db,
    }
    per_band = {
        "in_db": c.in_db,
        "out_db": c.out_db,
        "gr_mean_db": c.gr_mean_db,
        "gr_p50_db": c.gr_p50_db,  # a band averaging 2 dB with a p95 of 10 is pumping
        "gr_p95_db": c.gr_p95_db,
    }
    # The level change includes the band's makeup; its compressor's reduction does not.
    makeup = r.band_makeup_db or (0.0,) * len(c.in_db)
    for name in ("gr_mean_db", "gr_p50_db", "gr_p95_db"):
        per_band[name] = tuple(g + m for g, m in zip(per_band[name], makeup, strict=True))
    for name, values in per_band.items():
        for k, value in enumerate(values, start=1):
            row[f"band.{k}.{name}"] = value
    # The same octaves for every edge set, so balance compares between songs fairly.
    octave_in, octave_out = bands.octave_levels(r.inp, r.fs), bands.octave_levels(r.out, r.fs)
    for i, label in enumerate(bands.OCTAVE_LABELS):
        row[f"octave.{label}hz.in_db"] = float(octave_in[i])
        row[f"octave.{label}hz.out_db"] = float(octave_out[i])
    return row


def _programme(r: Rendered) -> dict[str, float]:
    psr = dynamics.psr(r.out, r.fs)
    lufs = loudness.integrated(r.out, r.fs)
    peak = loudness.true_peak(r.out, r.fs)
    row = {
        "lufs_i": lufs,
        "loudness_change_lu": lufs - loudness.integrated(r.inp, r.fs),
        "lra_lu": loudness.loudness_range(r.out, r.fs),
        "lra_change_lu": loudness.loudness_range(r.out, r.fs)
        - loudness.loudness_range(r.inp, r.fs),
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
    row |= _gauge_spread(r)
    if r.band_edges:
        row |= _bands(r)
    if r.decoded is not None:
        row["codec_true_peak_dbtp"] = loudness.true_peak(r.decoded, r.fs)
        row["codec_true_overs"] = float(dynamics.true_overs(r.decoded, r.fs, r.ceiling_dbtp))
    return row


def _recovery(r: Rendered) -> dict[str, float]:
    """Per playlist song: seconds from its start until the checked gauge comes within 1 dB of
    the correction that song needs (target minus its level, inside the gauge's limits); nan,
    and counted as missed, if it never does before the song ends."""
    if r.level_check is None or r.level_check[0] not in r.gauges:
        return {}
    name, target = r.level_check
    times, gain = r.gauges[name]
    low, high = r.gauge_limits.get(name, (-math.inf, math.inf))
    row: dict[str, float] = {}
    reached, missed = [], 0
    for i, seg in enumerate(r.segments, start=1):
        need = min(high, max(low, target - seg.level_dbfs))
        inside = (times >= seg.start_s) & (times < seg.stop_s)
        close = np.abs(gain[inside] - need) <= PINNED_WITHIN_DB
        if close.any():
            seconds = float(times[inside][np.argmax(close)] - seg.start_s)
            reached.append(seconds)
        else:
            seconds, missed = math.nan, missed + 1
        row[f"recovery_s.{i:02d}-{seg.label}"] = seconds
    row["recovery_s_max"] = max(reached) if reached else math.nan
    row["recovery_s_median"] = float(np.median(reached)) if reached else math.nan
    row["recovery_missed"] = float(missed)
    return row


def _sequence(r: Rendered) -> dict[str, float]:
    """A playlist: each song's output loudness and their spread (the AGC's song-to-song job),
    and how fast the gain reaches each song's correction."""
    row: dict[str, float] = {}
    for i, seg in enumerate(r.segments, start=1):
        part = r.out[round(seg.start_s * r.fs) : round(seg.stop_s * r.fs)]
        row[f"item_lufs.{i:02d}-{seg.label}"] = loudness.integrated(part, r.fs)
    levels = [v for k, v in row.items() if math.isfinite(v)]
    row["item_lufs_spread"] = max(levels) - min(levels) if levels else math.nan
    return row | _recovery(r)


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
    SEQUENCE: _sequence,
}


def measure(kind: str, rendered: Rendered) -> dict[str, float]:
    """The metrics for one render of an input of this kind."""
    if kind not in MEASURES:
        raise KeyError(f"no measurement for input kind {kind!r}")
    return MEASURES[kind](rendered)
