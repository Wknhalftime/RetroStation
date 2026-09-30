"""What the backend sends Liquidsoap for one item (D23 no-cues defaults; D26 the final hook).

Liquidsoap always needs a valid ``liq_cue_out`` greater than ``liq_cue_in``: a no-cues song
plays to its end with no overlap and no gain change (D23), and the interim PR G sign-off
hook (D26) sends a whole clip the same way, marked ``final``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from backend.domain.streaming import InvalidStreamValueError, ScheduleItem, StreamTiming
from backend.playout.liquidsoap_process import long_path

__all__ = [
    "NO_CUES_GAIN_DB",
    "NO_CUES_START_NEXT_MS",
    "FinalClip",
    "ItemPayload",
    "final_payload",
    "item_payload",
]

NO_CUES_START_NEXT_MS = 0
"""D23: a no-cues song has no overlap with the next item."""

NO_CUES_GAIN_DB = 0.0
"""D23: a no-cues song plays at its recorded level, unchanged."""


@dataclass(frozen=True)
class ItemPayload:
    """One item's Liquidsoap annotations, keyed exactly as the contract sends them."""

    path: str
    annotations: Mapping[str, str]


@dataclass(frozen=True)
class FinalClip:
    """The PR G sign-off clip; PR D only wires the hook (D26), so no clip ships yet."""

    path: Path
    span_ms: int

    def __post_init__(self) -> None:
        if self.span_ms <= 0:
            raise InvalidStreamValueError(f"FinalClip.span_ms must be > 0, got {self.span_ms}")


def _seconds(ms: int) -> str:
    """``ms`` formatted exactly as Liquidsoap's annotations expect: ``"S.mmm"``."""
    return f"{ms // 1000}.{ms % 1000:03d}"


def item_payload(seq: int, item: ScheduleItem, offset_ms: int, timing: StreamTiming) -> ItemPayload:
    """The annotations for one logged play, landing ``offset_ms`` into its cue-in (D23)."""
    file = item.file
    if file is None:
        raise InvalidStreamValueError(f"item_payload: item {item.event_id} has no resolved file")
    cues = file.cues
    cue_in_ms = (cues.cue_in_ms if cues is not None else 0) + offset_ms
    cue_out_ms = cues.cue_out_ms if cues is not None else (file.duration_ms or 0)
    fade_in_ms = cues.fade_in_ms if cues is not None else timing.default_fade_in_ms
    fade_out_ms = cues.fade_out_ms if cues is not None else timing.default_fade_out_ms
    start_next_ms = cues.start_next_ms if cues is not None else NO_CUES_START_NEXT_MS
    gain_db = cues.gain_db if cues is not None else NO_CUES_GAIN_DB
    annotations = {
        "item_seq": str(seq),
        "liq_cue_in": _seconds(cue_in_ms),
        "liq_cue_out": _seconds(cue_out_ms),
        "liq_fade_in": _seconds(fade_in_ms),
        "liq_fade_out": _seconds(fade_out_ms),
        "sn_rem": _seconds(start_next_ms),
        "liq_amplify": f"{gain_db:.1f} dB",
        "title": item.title,
        "artist": item.artist,
    }
    return ItemPayload(path=long_path(Path(file.path)), annotations=annotations)


def final_payload(seq: int, clip: FinalClip) -> ItemPayload:
    """The sign-off clip's annotations: zero fades, no gain change, marked ``final`` (D26)."""
    annotations = {
        "item_seq": str(seq),
        "liq_cue_in": _seconds(0),
        "liq_cue_out": _seconds(clip.span_ms),
        "liq_fade_in": _seconds(0),
        "liq_fade_out": _seconds(0),
        "sn_rem": _seconds(0),
        "liq_amplify": f"{NO_CUES_GAIN_DB:.1f} dB",
        "title": "",
        "artist": "",
        "final": "true",
    }
    return ItemPayload(path=long_path(clip.path), annotations=annotations)
