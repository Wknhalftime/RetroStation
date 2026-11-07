"""Song endings: where EOM goes and how the old song leaves (EOM ending rules, stage 1).

Spec: docs/superpowers/specs/2026-11-07-eom-ending-rules-design.md; plan
docs/superpowers/plans/2026-11-07-eom-ending-rules-stage1.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from backend.domain.streaming import InvalidStreamValueError


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
