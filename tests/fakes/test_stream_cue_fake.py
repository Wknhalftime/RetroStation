"""Behaviour of the stream cue fake, so tests and PR E can rely on it (spec D20).

Like the real table: one analysis per audio hash, the latest wins, and no file is needed.

DRAFT for D20: replaces ``test_stream_cue_fake.py`` once the user approves.
"""

from __future__ import annotations

from backend.domain.library import AudioHash
from backend.domain.streaming import CueAnalysis, CuePoints
from tests.fakes.stream_cues import FakeStreamCueRepository

AUDIO = AudioHash.parse("flac-md5:" + "0123456789abcdef" * 2)
OTHER = AudioHash.parse("audio-sha256:" + "ab" * 32)


def analysis(audio: AudioHash = AUDIO, *, failed: bool = False) -> CueAnalysis:
    return CueAnalysis(
        audio_hash=audio,
        cues=CuePoints(
            cue_in_ms=0,
            cue_out_ms=100_000,
            fade_in_ms=1_000,
            fade_out_ms=1_000,
            start_next_ms=1_000,
            gain_db=-2.0,
        ),
        loudness_lufs=None,
        analysis_failed=failed,
        analyser_version=1,
    )


def test_cue_fake_keeps_the_latest_analysis_per_audio() -> None:
    repo = FakeStreamCueRepository()
    repo.upsert(analysis())
    repo.upsert(analysis(failed=True))

    assert repo.analyses == {AUDIO: analysis(failed=True)}


def test_cue_fake_keeps_each_audio_apart() -> None:
    repo = FakeStreamCueRepository()
    repo.upsert(analysis(AUDIO))
    repo.upsert(analysis(OTHER, failed=True))

    assert repo.analyses == {AUDIO: analysis(AUDIO), OTHER: analysis(OTHER, failed=True)}
