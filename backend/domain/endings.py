"""Song endings: where EOM goes and how the old song leaves (EOM ending rules, stage 1).

Spec: docs/superpowers/specs/2026-11-07-eom-ending-rules-design.md; plan
docs/superpowers/plans/2026-11-07-eom-ending-rules-stage1.md.

Lance's rules (2026-11-07): a cold ending keeps EOM on the last hit and stops on its own; a
sustained ending's EOM goes where the full-volume part ends; the old song fades out after EOM,
1 s when a liner follows, 2 s after a sustained ending, 1 s after a fade. Loudness cannot tell a
sustained ending from a fade, so stage 1 has three measured types:

- ``cold``: the level drops ``cold_drop_db`` or more within ``cold_window_ms`` after autocue's
  EOM. EOM and Stop stay autocue's; no fade.
- ``long_tail``: the full-volume part ends ``min_gap_ms`` or more before autocue's EOM. EOM moves
  to ``after_body_ms`` after the end of the full-volume part; Lance's sustained fade.
- ``other``: everything else, mostly fades. EOM stays autocue's; Lance's fade.

``unmeasured`` audio could not be read and plays autocue's points unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType

from backend.domain.library import AudioHash
from backend.domain.streaming import CuePoints, InvalidStreamValueError

ENDING_ANALYSER_VERSION = 1
"""Version of the ending analysis in force, recorded on each row; an analysis change purges
the rows of other versions (as D20 does for autocue's rows)."""

_SILENCE_DB = -120.0
"""Frames past the end of the tail count as silence: a song that stops as the file ends."""


class EndingType(StrEnum):
    COLD = "cold"
    LONG_TAIL = "long_tail"
    OTHER = "other"
    UNMEASURED = "unmeasured"


class Follows(StrEnum):
    """What plays after the song: it sets the length of the fade after EOM."""

    SONG = "song"
    LINER = "liner"


FADE_AFTER_EOM_MS: Mapping[tuple[EndingType, Follows], int] = MappingProxyType(
    {
        (EndingType.LONG_TAIL, Follows.SONG): 2000,
        (EndingType.LONG_TAIL, Follows.LINER): 1000,
        (EndingType.OTHER, Follows.SONG): 1000,
        (EndingType.OTHER, Follows.LINER): 1000,
    }
)
"""Lance's fade-out of the old song after EOM (2026-11-07). A cold ending has none."""


@dataclass(frozen=True)
class LoudnessTail:
    """The end of a song's loudness: 50 ms frames from ``start_ms`` to the end of the file,
    each in dB relative to the whole song's mean energy (``backend.playout.loudness``)."""

    start_ms: int
    frame_ms: int
    level_db: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.start_ms < 0:
            raise InvalidStreamValueError(
                f"LoudnessTail.start_ms must be >= 0, got {self.start_ms}"
            )
        if self.frame_ms <= 0:
            raise InvalidStreamValueError(f"LoudnessTail.frame_ms must be > 0, got {self.frame_ms}")
        if not self.level_db:
            raise InvalidStreamValueError("LoudnessTail.level_db must hold at least one frame")
        if not all(math.isfinite(level) for level in self.level_db):
            raise InvalidStreamValueError("LoudnessTail.level_db must be finite")


@dataclass(frozen=True)
class SongEnding:
    """A song's measured ending. ``eom_ms`` is the long tail's EOM, and only a long tail has
    one. ``measured_cue_out_ms`` is autocue's Stop when it was measured: a rule applies only
    while autocue's row still has that Stop."""

    ending: EndingType
    eom_ms: int | None
    measured_cue_out_ms: int

    def __post_init__(self) -> None:
        if self.ending is EndingType.LONG_TAIL:
            if self.eom_ms is None or self.eom_ms < 0:
                raise InvalidStreamValueError(
                    f"SongEnding: a long tail needs an EOM >= 0, got {self.eom_ms}"
                )
        elif self.eom_ms is not None:
            raise InvalidStreamValueError(
                f"SongEnding: only a long tail has an EOM, got {self.ending} with {self.eom_ms}"
            )
        if self.measured_cue_out_ms < 0:
            raise InvalidStreamValueError(
                f"SongEnding.measured_cue_out_ms must be >= 0, got {self.measured_cue_out_ms}"
            )


@dataclass(frozen=True)
class EndingRules:
    """The classifier's settings. ``cold_*`` is the plan's rule; the rest were tuned on the
    tuning half of Lance's picks (Task 3, step 4)."""

    cold_drop_db: float = 15.0
    cold_window_ms: int = 1000
    body_db: float = -2.0
    min_gap_ms: int = 3000
    after_body_ms: int = 250
    smoothing_ms: int = 400


@dataclass(frozen=True)
class EndingCandidate:
    """Audio whose ending needs measuring: one file it is read from, and autocue's points."""

    audio_hash: AudioHash
    path: str
    cues: CuePoints


def _autocue_eom_ms(cues: CuePoints) -> int:
    return cues.cue_out_ms - cues.start_next_ms


def _smoothed(levels: list[float], width: int) -> list[float]:
    """Each frame's trailing mean energy over ``width`` frames, in dB."""
    out: list[float] = []
    total = 0.0
    energies = [10 ** (level / 10) for level in levels]
    for index, energy in enumerate(energies):
        total += energy
        if index >= width:
            total -= energies[index - width]
        count = min(index + 1, width)
        out.append(10 * math.log10(max(total, 1e-30) / count))
    return out


DEFAULT_RULES = EndingRules()
"""The tuned settings the radio uses."""


def classify_ending(
    tail: LoudnessTail, cues: CuePoints, rules: EndingRules = DEFAULT_RULES
) -> SongEnding:
    """The ending of a song with autocue's ``cues``, read from its loudness ``tail``."""
    frame = tail.frame_ms

    def index(ms: int) -> int:
        return max(0, (ms - tail.start_ms) // frame)

    autocue = _autocue_eom_ms(cues)
    after = index(autocue + rules.cold_window_ms)
    levels = list(tail.level_db) + [_SILENCE_DB] * max(0, after + 1 - len(tail.level_db))
    level = _smoothed(levels, max(1, rules.smoothing_ms // frame))
    if level[index(autocue)] - level[after] >= rules.cold_drop_db:
        return SongEnding(EndingType.COLD, None, cues.cue_out_ms)
    stop = min(index(cues.cue_out_ms), len(tail.level_db))
    body = [i for i in range(stop) if level[i] > rules.body_db]
    if body:
        body_end_ms = tail.start_ms + body[-1] * frame
        if autocue - body_end_ms >= rules.min_gap_ms:
            return SongEnding(
                EndingType.LONG_TAIL, body_end_ms + rules.after_body_ms, cues.cue_out_ms
            )
    return SongEnding(EndingType.OTHER, None, cues.cue_out_ms)


def _applies(stored: CuePoints, ending: SongEnding) -> bool:
    """A rule applies to a measured ending while autocue's row keeps the Stop it measured."""
    return (
        ending.ending is not EndingType.UNMEASURED
        and stored.cue_out_ms == ending.measured_cue_out_ms
    )


def _with_eom(cues: CuePoints, eom_ms: int, fade_ms: int) -> CuePoints:
    """``cues`` with the next song at ``eom_ms`` and a fade of ``fade_ms`` from it, Stop no
    later than ``cues``'; ``cues`` unchanged when ``eom_ms`` is outside the span."""
    if not cues.cue_in_ms < eom_ms < cues.cue_out_ms:
        return cues
    cue_out_ms = min(cues.cue_out_ms, eom_ms + fade_ms)
    return replace(
        cues,
        cue_out_ms=cue_out_ms,
        fade_out_ms=cue_out_ms - eom_ms,
        start_next_ms=cue_out_ms - eom_ms,
    )


def ruled_cues(stored: CuePoints, ending: SongEnding, follows: Follows) -> CuePoints:
    """What the radio plays for a song with autocue's ``stored`` points and ``ending``, when
    ``follows`` plays next. ``stored`` itself when the rule does not apply."""
    if not _applies(stored, ending):
        return stored
    autocue = _autocue_eom_ms(stored)
    if ending.ending is EndingType.COLD:
        return replace(stored, fade_out_ms=0, start_next_ms=stored.cue_out_ms - autocue)
    eom_ms = ending.eom_ms if ending.eom_ms is not None else autocue
    return _with_eom(stored, eom_ms, FADE_AFTER_EOM_MS[(ending.ending, follows)])


def before_liner(cues: CuePoints, ending: SongEnding) -> CuePoints:
    """A long tail's song-to-song ``cues`` shortened for a liner next. Anything else, or cues
    the rule did not make (an edit, a stale measurement), comes back unchanged."""
    if ending.ending is not EndingType.LONG_TAIL or ending.eom_ms is None:
        return cues
    eom_ms = _autocue_eom_ms(cues)
    if eom_ms != ending.eom_ms or cues.fade_out_ms != cues.start_next_ms:
        return cues
    return _with_eom(cues, eom_ms, FADE_AFTER_EOM_MS[(EndingType.LONG_TAIL, Follows.LINER)])
