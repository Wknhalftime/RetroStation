"""Which cues a landing plays (spec: D9, "A song is playable only if its playable span is at
least fade_in + fade_out + 3 s ... The same span is the minimum landing tail"; D80, "A tail
left shorter than D9's minimum by the landing offset (R8) plays with D23's values")."""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.domain.streaming import CuePoints, PlayableFile, StreamTiming

CUES = CuePoints(
    cue_in_ms=1_500,
    cue_out_ms=181_000,
    fade_in_ms=2_000,
    fade_out_ms=5_000,
    start_next_ms=4_500,
    gain_db=-6.2,
)
"""A span of 179.5 s; D9's minimum with these fades is 2 + 5 + 3 = 10 s, so the last offset
that leaves it is 169.5 s."""
TIMING = StreamTiming()


def file_with(cues: CuePoints | None) -> PlayableFile:
    return PlayableFile(file_id=uuid4(), path="D:/Music/a.flac", duration_ms=200_000, cues=cues)


@pytest.mark.parametrize("offset_ms", [0, 60_000, 169_500])
def test_a_landing_that_leaves_the_minimum_plays_its_cues(offset_ms: int) -> None:
    """D9: exactly the minimum span left is enough."""
    assert file_with(CUES).cues_to_play(offset_ms, TIMING) == CUES


def test_a_landing_that_leaves_less_than_the_minimum_plays_no_cues() -> None:
    """D80: one millisecond short of D9's minimum, the cues are dropped, so D23's values play."""
    assert file_with(CUES).cues_to_play(169_501, TIMING) is None


def test_a_song_without_cues_has_none_to_play() -> None:
    """D23 applies as before: there is nothing to drop."""
    assert file_with(None).cues_to_play(0, TIMING) is None
