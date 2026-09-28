"""Behaviour of the stream cue fake, so tests and PR E can rely on it."""

from __future__ import annotations

from uuid import uuid4

from backend.domain.streaming import CueAnalysis, CuePoints
from tests.fakes.stream_cues import FakeStreamCueRepository

FILE = uuid4()


def analysis(*, failed: bool = False) -> CueAnalysis:
    return CueAnalysis(
        file_id=FILE,
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
        file_size=None,
        file_mtime_ns=None,
    )


def test_cue_fake_keeps_the_latest_analysis_per_file() -> None:
    repo = FakeStreamCueRepository()
    repo.upsert(analysis())
    repo.upsert(analysis(failed=True))

    assert repo.analyses == {FILE: analysis(failed=True)}
