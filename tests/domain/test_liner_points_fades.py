"""Station liners: a liner never plays with a fade-in or fade-out of autocue's (LR4)."""

from __future__ import annotations

from backend.domain.library import AudioHash
from backend.domain.liners import liner_points
from backend.domain.streaming import CueAnalysis, CuePoints

HASH = AudioHash.parse("flac-md5:" + "c" * 32)
STORED = CuePoints(120, 6_900, 500, 400, 300, -3.5)
NO_FADES = CuePoints(120, 6_900, 0, 0, 300, -3.5)


def stored_row(*, failed: bool) -> CueAnalysis:
    return CueAnalysis(
        audio_hash=HASH,
        cues=STORED,
        loudness_lufs=None if failed else -14.0,
        analysis_failed=failed,
        analyser_version=1,
    )


def test_measured_row_loses_fade_in_and_fade_out() -> None:
    assert liner_points(7_000, None, stored_row(failed=False)) == NO_FADES


def test_failed_row_loses_fade_in_and_fade_out() -> None:
    assert liner_points(7_000, None, stored_row(failed=True)) == NO_FADES
