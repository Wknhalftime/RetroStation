"""Cues re-read just before a song is queued (spec: D85, "Cues are re-read for each song just
before it is queued, so cues stored mid-session take effect on the song's next play"; the D85
brief: "A missing row means no cues ... A stored row turns the item into a cued one"; D9, a
song handed out must stay playable)."""

from __future__ import annotations

from uuid import uuid4

from backend.domain.streaming import CuePoints, PlayableFile, StreamTiming

STORED = CuePoints(
    cue_in_ms=1_500,
    cue_out_ms=181_000,
    fade_in_ms=2_000,
    fade_out_ms=5_000,
    start_next_ms=4_500,
    gain_db=-6.2,
)
TOO_SHORT = CuePoints(
    cue_in_ms=0, cue_out_ms=9_000, fade_in_ms=3_000, fade_out_ms=4_000, start_next_ms=0, gain_db=0.0
)
"""A span of 9 s, under D9's minimum of 3 + 4 + 3 = 10 s."""
TIMING = StreamTiming()


def file_with(cues: CuePoints | None, duration_ms: int | None = 200_000) -> PlayableFile:
    return PlayableFile(file_id=uuid4(), path="D:/Music/a.flac", duration_ms=duration_ms, cues=cues)


def test_stored_cues_turn_a_song_into_a_cued_one() -> None:
    """D85: a row stored since tune-in is played on the song's next play."""
    tuned_in = file_with(None)
    assert tuned_in.with_stored_cues(STORED, TIMING) == PlayableFile(
        tuned_in.file_id, tuned_in.path, tuned_in.duration_ms, STORED
    )


def test_no_stored_row_makes_a_cued_song_uncued() -> None:
    """D85 with D20: a missing row means no cues (an analyser change purged it, or the hash
    was cleared), so D23's values play and the song is reported (D78, D79)."""
    assert file_with(STORED).with_stored_cues(None, TIMING).cues is None


def test_stored_cues_that_would_make_the_song_unplayable_are_ignored() -> None:
    """D9: the walk chose this song as playable; it keeps the values read at tune-in."""
    tuned_in = file_with(None)
    assert tuned_in.with_stored_cues(TOO_SHORT, TIMING) == tuned_in


def test_no_row_for_a_song_with_no_duration_keeps_its_cues() -> None:
    """Without cues a song with no duration has no span (D9), so the tune-in values stay."""
    tuned_in = file_with(STORED, duration_ms=None)
    assert tuned_in.with_stored_cues(None, TIMING) == tuned_in
