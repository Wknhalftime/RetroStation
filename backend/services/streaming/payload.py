"""What the backend sends Liquidsoap for one item (D23 no-cues defaults; D26 the sign-off).

Liquidsoap always needs a valid ``liq_cue_out`` greater than ``liq_cue_in``: a no-cues song
plays to its end with no overlap and no gain change (D23), and the sign-off clip (D26) is
sent whole the same way, with zero fades and no title (PG14), marked ``final``.
"""

from __future__ import annotations

import dataclasses
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
    "landing_payload",
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
    """The clip that signs the station off (D26): the user's own sign-off, or a configured
    default; ``span_ms`` is how long it plays, from its start."""

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
    if cues is None:
        cue_in_ms = offset_ms
        cue_out_ms = file.duration_ms or 0
        fade_in_ms = timing.default_fade_in_ms
        fade_out_ms = timing.default_fade_out_ms
        start_next_ms = NO_CUES_START_NEXT_MS
        gain_db = NO_CUES_GAIN_DB
    else:
        cue_in_ms = cues.cue_in_ms + offset_ms
        cue_out_ms = cues.cue_out_ms
        fade_in_ms = cues.fade_in_ms
        fade_out_ms = cues.fade_out_ms
        start_next_ms = cues.start_next_ms
        gain_db = cues.gain_db
    if cue_out_ms <= cue_in_ms:
        raise InvalidStreamValueError(
            f"item_payload: liq_cue_out ({cue_out_ms}) must be greater than liq_cue_in "
            f"({cue_in_ms}); check file.duration_ms ({file.duration_ms!r}) against "
            f"offset_ms ({offset_ms}) for item {item.event_id}"
        )
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


def landing_payload(
    seq: int, item: ScheduleItem, offset_ms: int, timing: StreamTiming
) -> ItemPayload:
    """The annotations for an item played from ``offset_ms``: ``item_payload``, or, when
    ``cues_to_play`` drops the cues (D80), ``item_payload`` of the item without cues from
    ``offset_ms + cues.cue_in_ms``, the same moment of the song on the file's own timeline
    (coordinator ruling, Revision 2). An item with no file, or with no cues, goes to
    ``item_payload`` unchanged (no file raises, as today)."""
    file = item.file
    if file is None or file.cues is None or file.cues_to_play(offset_ms, timing) is not None:
        return item_payload(seq, item, offset_ms, timing)
    no_cue_offset_ms = offset_ms + file.cues.cue_in_ms
    no_cue_item = dataclasses.replace(item, file=dataclasses.replace(file, cues=None))
    return item_payload(seq, no_cue_item, no_cue_offset_ms, timing)


def final_payload(seq: int, clip: FinalClip) -> ItemPayload:
    """The sign-off clip's annotations: whole, at 0 dB, with zero fades and no title (PG14),
    marked ``final`` (D26)."""
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
