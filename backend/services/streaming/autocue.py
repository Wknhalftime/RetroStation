"""Liquidsoap's autocue output read as cue points: the reverse of payload.py.

Spec: Cue pre-computation ("autocue.internal, lufs_target -18, amplify_behavior "keep"");
Data (start_next_ms "counted back from cue_out"; gain_db "gain to reach -18 LUFS"). House
rule: validate on load and name the field, so every predictable failure is checked before a
CuePoints is built. D54 rules ``start_next_ms`` = ``liq_cross_end_duration``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from backend.domain.streaming import CuePoints, InvalidStreamValueError

AUTOCUE_KEYS = (
    "liq_cue_in",
    "liq_cue_out",
    "liq_fade_in",
    "liq_fade_out",
    "liq_cross_end_duration",
    "liq_amplify",
)
"""The autocue metadata keys a cue analysis needs, in the test contract's order."""

_SECONDS_KEYS = tuple(key for key in AUTOCUE_KEYS if key != "liq_amplify")
_GAIN_PATTERN = re.compile(r"^-?\d+(\.\d+)? dB$")


@dataclass(frozen=True)
class Unusable:
    """Autocue output that cannot become cue points; ``reason`` names the field."""

    reason: str


def _seconds_to_ms(key: str, value: str) -> int | Unusable:
    """``value`` (seconds, as autocue reports it) as whole milliseconds, or why not."""
    try:
        seconds = float(value)
    except ValueError:
        return Unusable(f"{key} is not a number of seconds: {value!r}")
    if not math.isfinite(seconds):
        return Unusable(f"{key} is not a number of seconds: {value!r}")
    return round(seconds * 1000)


def _gain_db(value: str) -> float | Unusable:
    """``liq_amplify`` (e.g. ``"-6.2 dB"``) as a finite gain, or why not."""
    if _GAIN_PATTERN.fullmatch(value) is None:
        return Unusable(f"liq_amplify is not a gain in dB: {value!r}")
    gain = float(value.removesuffix(" dB"))
    if not math.isfinite(gain):
        return Unusable(f"liq_amplify is not a gain in dB: {value!r}")
    return gain


def autocue_points(metadata: Mapping[str, str]) -> CuePoints | Unusable:
    """The cue points autocue's output encodes, or why they cannot be used."""
    if not metadata:
        return Unusable("autocue returned no metadata")
    for key in AUTOCUE_KEYS:
        if key not in metadata:
            return Unusable(f"missing {key}")
    milliseconds: dict[str, int] = {}
    for key in _SECONDS_KEYS:
        ms = _seconds_to_ms(key, metadata[key])
        if isinstance(ms, Unusable):
            return ms
        milliseconds[key] = ms
    gain = _gain_db(metadata["liq_amplify"])
    if isinstance(gain, Unusable):
        return gain
    try:
        return CuePoints(
            cue_in_ms=milliseconds["liq_cue_in"],
            cue_out_ms=milliseconds["liq_cue_out"],
            fade_in_ms=milliseconds["liq_fade_in"],
            fade_out_ms=milliseconds["liq_fade_out"],
            start_next_ms=milliseconds["liq_cross_end_duration"],
            gain_db=gain,
        )
    except InvalidStreamValueError as error:
        return Unusable(str(error))
