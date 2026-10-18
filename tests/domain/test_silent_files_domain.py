"""Acceptance tests: CueAnalysis carries the silent marker (D111, S3).

A silent row is a fallback row: audio_silent requires analysis_failed, so the store can never
hold a "silent" row with measured cues. The marker defaults to false, so every existing writer
keeps its meaning. Read as an attribute / keyword, so before the build each test fails alone.
"""

from __future__ import annotations

import pytest

from backend.domain.library import AudioHash
from backend.domain.streaming import CueAnalysis, CuePoints, InvalidStreamValueError

AUDIO = AudioHash.parse("flac-md5:" + "0123456789abcdef" * 2)
FALLBACK = CuePoints(
    cue_in_ms=0,
    cue_out_ms=200_000,
    fade_in_ms=3_000,
    fade_out_ms=4_000,
    start_next_ms=0,
    gain_db=-8.0,
)


def test_audio_silent_defaults_to_false() -> None:
    assert CueAnalysis(AUDIO, FALLBACK, None, False, 1).audio_silent is False


def test_a_silent_analysis_is_a_failed_one() -> None:
    analysis = CueAnalysis(AUDIO, FALLBACK, None, True, 1, audio_silent=True)
    assert (analysis.analysis_failed, analysis.audio_silent) == (True, True)


def test_silent_audio_with_measured_cues_is_refused() -> None:
    with pytest.raises(InvalidStreamValueError, match=r"CueAnalysis\.audio_silent"):
        CueAnalysis(AUDIO, FALLBACK, None, False, 1, audio_silent=True)
