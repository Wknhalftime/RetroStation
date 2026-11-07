"""The ending domain (EOM ending rules, stage 1, Task 3): types, classifier and ruled cues.

Spec: docs/superpowers/specs/2026-11-07-eom-ending-rules-design.md; plan Task 3. Lance's fade
after EOM (2026-11-07): a liner next 1 s; after a sustained (long-tail) ending 2 s; after a fade
1 s; a cold ending none. The classifier reads 50 ms frames smoothed over 400 ms.
"""

from __future__ import annotations

import uuid

import pytest

from backend.domain.endings import (
    ENDING_ANALYSER_VERSION,
    FADE_AFTER_EOM_MS,
    EndingRules,
    EndingType,
    Follows,
    LoudnessTail,
    SongEnding,
    before_liner,
    classify_ending,
    ruled_cues,
)
from backend.domain.streaming import CuePoints, InvalidStreamValueError, PlayableFile

FRAME = 50


def tail_of(start_ms: int, end_ms: int, level_at: object) -> LoudnessTail:
    """A tail of 50 ms frames from ``start_ms`` to ``end_ms``; ``level_at(ms)`` gives each
    frame's level in dB relative to the song's mean."""
    assert callable(level_at)
    return LoudnessTail(
        start_ms,
        FRAME,
        tuple(float(level_at(ms)) for ms in range(start_ms, end_ms, FRAME)),
    )


def cues(cue_out_ms: int, start_next_ms: int, *, fade_out_ms: int = 4_000) -> CuePoints:
    return CuePoints(
        cue_in_ms=0,
        cue_out_ms=cue_out_ms,
        fade_in_ms=0,
        fade_out_ms=fade_out_ms,
        start_next_ms=start_next_ms,
        gain_db=-3.0,
    )


def cliff_at_100s(ms: int) -> float:
    return 0.0 if ms < 100_000 else -40.0


def ring_after_100s(ms: int) -> float:
    """Full volume until 100 s, then a ring 6 dB down that decays 0.5 dB a second."""
    return 0.0 if ms < 100_000 else -6.0 - 0.5 * (ms - 100_000) / 1000


# ---- classify_ending ------------------------------------------------------------------------

PROTOTYPE = EndingRules(
    cold_drop_db=15.0,
    cold_window_ms=1000,
    body_db=-2.0,
    min_gap_ms=2500,
    after_body_ms=500,
    smoothing_ms=400,
)
"""The prototype's values (2026-11-07), passed explicitly so tuning the defaults (Task 3,
step 4) never breaks these tests of how the classifier reads a tail."""


def test_cliff_after_autocue_eom_is_cold() -> None:
    song = cues(101_000, 1_200)  # autocue's EOM at 99.8 s
    ending = classify_ending(tail_of(40_000, 102_000, cliff_at_100s), song, PROTOTYPE)
    assert ending == SongEnding(EndingType.COLD, None, 101_000)


def test_long_ring_is_long_tail() -> None:
    song = cues(115_000, 7_000)  # autocue's EOM at 108 s, 8 s into the ring
    ending = classify_ending(tail_of(55_000, 116_000, ring_after_100s), song, PROTOTYPE)
    assert ending.ending is EndingType.LONG_TAIL
    assert ending.measured_cue_out_ms == 115_000
    # The body ends where the 400 ms smoothing falls below -2 dB (about 200 ms after the
    # step), and EOM is 500 ms after that.
    assert ending.eom_ms is not None
    assert 100_500 <= ending.eom_ms <= 100_800


def test_body_close_to_autocue_is_other() -> None:
    song = cues(115_000, 13_500)  # autocue's EOM at 101.5 s, under 2.5 s after the body
    ending = classify_ending(tail_of(55_000, 116_000, ring_after_100s), song, PROTOTYPE)
    assert ending == SongEnding(EndingType.OTHER, None, 115_000)


def test_false_ending_keeps_the_final_cold_stop() -> None:
    """A dip to silence mid-song is not the ending: the final cliff is."""

    def false_stop(ms: int) -> float:
        if 60_000 <= ms < 62_000:
            return -40.0
        return cliff_at_100s(ms)

    song = cues(101_000, 1_200)
    ending = classify_ending(tail_of(41_000, 102_000, false_stop), song, PROTOTYPE)
    assert ending.ending is EndingType.COLD
    played = ruled_cues(song, ending, Follows.SONG)
    assert played.cue_out_ms - played.start_next_ms == 99_800


def test_cold_rule_defaults() -> None:
    """The cold rule is the plan's, not tuned: 15 dB or more within 1 s."""
    rules = EndingRules()
    assert (rules.cold_drop_db, rules.cold_window_ms) == (15.0, 1000)


def test_cliff_at_end_of_file_is_cold() -> None:
    """The file ends 0.4 s after autocue's EOM: frames past the tail's end count as
    silence, so the drop within 1 s is still seen."""
    song = cues(100_200, 400)  # autocue's EOM at 99.8 s; the file ends at 100.2 s
    ending = classify_ending(tail_of(40_000, 100_200, lambda ms: 0.0), song, PROTOTYPE)
    assert ending == SongEnding(EndingType.COLD, None, 100_200)


# ---- ruled_cues -----------------------------------------------------------------------------

STORED = cues(200_000, 5_000)  # autocue's EOM at 195 s
"""Autocue's points: Stop at 200 s, a 4 s fade, the next song 5 s before Stop."""


def test_fade_table() -> None:
    assert FADE_AFTER_EOM_MS[(EndingType.LONG_TAIL, Follows.SONG)] == 2000
    assert FADE_AFTER_EOM_MS[(EndingType.LONG_TAIL, Follows.LINER)] == 1000
    assert FADE_AFTER_EOM_MS[(EndingType.OTHER, Follows.SONG)] == 1000
    assert FADE_AFTER_EOM_MS[(EndingType.OTHER, Follows.LINER)] == 1000


def test_cold_keeps_stop_and_drops_fade() -> None:
    played = ruled_cues(STORED, SongEnding(EndingType.COLD, None, 200_000), Follows.SONG)
    assert (played.cue_out_ms, played.fade_out_ms, played.start_next_ms) == (200_000, 0, 5_000)


def test_cold_before_liner_is_the_same() -> None:
    ending = SongEnding(EndingType.COLD, None, 200_000)
    assert ruled_cues(STORED, ending, Follows.LINER) == ruled_cues(STORED, ending, Follows.SONG)


def test_long_tail_song_fade_is_two_seconds() -> None:
    played = ruled_cues(STORED, SongEnding(EndingType.LONG_TAIL, 190_000, 200_000), Follows.SONG)
    assert (played.cue_out_ms, played.fade_out_ms, played.start_next_ms) == (192_000, 2000, 2000)


def test_long_tail_liner_fade_is_one_second() -> None:
    played = ruled_cues(STORED, SongEnding(EndingType.LONG_TAIL, 190_000, 200_000), Follows.LINER)
    assert (played.cue_out_ms, played.fade_out_ms, played.start_next_ms) == (191_000, 1000, 1000)


def test_other_song_fade_is_one_second() -> None:
    played = ruled_cues(STORED, SongEnding(EndingType.OTHER, None, 200_000), Follows.SONG)
    assert (played.cue_out_ms, played.fade_out_ms, played.start_next_ms) == (196_000, 1000, 1000)


def test_rule_keeps_start_fade_in_and_gain() -> None:
    stored = CuePoints(1_500, 200_000, 200, 4_000, 5_000, -7.5)
    played = ruled_cues(stored, SongEnding(EndingType.OTHER, None, 200_000), Follows.SONG)
    assert (played.cue_in_ms, played.fade_in_ms, played.gain_db) == (1_500, 200, -7.5)


def test_fade_is_clamped_to_stored_stop() -> None:
    stored = cues(200_000, 500)  # autocue's EOM half a second before Stop
    played = ruled_cues(stored, SongEnding(EndingType.OTHER, None, 200_000), Follows.SONG)
    assert (played.cue_out_ms, played.fade_out_ms, played.start_next_ms) == (200_000, 500, 500)


@pytest.mark.parametrize("eom_ms", [0, 200_000, 250_000])
def test_long_tail_eom_outside_span_falls_back(eom_ms: int) -> None:
    ending = SongEnding(EndingType.LONG_TAIL, eom_ms, 200_000)
    assert ruled_cues(STORED, ending, Follows.SONG) == STORED


def test_long_tail_eom_at_or_before_start_falls_back() -> None:
    stored = CuePoints(5_000, 200_000, 0, 4_000, 5_000, -3.0)
    for eom_ms in (2_000, 5_000):
        ending = SongEnding(EndingType.LONG_TAIL, eom_ms, 200_000)
        assert ruled_cues(stored, ending, Follows.SONG) == stored


def test_stale_measurement_falls_back() -> None:
    ending = SongEnding(EndingType.LONG_TAIL, 190_000, 199_000)
    assert ruled_cues(STORED, ending, Follows.SONG) == STORED


def test_unmeasured_is_stored() -> None:
    ending = SongEnding(EndingType.UNMEASURED, None, 200_000)
    assert ruled_cues(STORED, ending, Follows.SONG) == STORED
    assert ruled_cues(STORED, ending, Follows.LINER) == STORED


# ---- before_liner ---------------------------------------------------------------------------


def test_before_liner_shortens_ruled_long_tail() -> None:
    ending = SongEnding(EndingType.LONG_TAIL, 190_000, 200_000)
    song = ruled_cues(STORED, ending, Follows.SONG)
    assert before_liner(song, ending) == ruled_cues(STORED, ending, Follows.LINER)


def test_before_liner_leaves_edit_shaped_cues() -> None:
    ending = SongEnding(EndingType.LONG_TAIL, 190_000, 200_000)
    edited = CuePoints(0, 188_000, 0, 2_000, 3_000, -3.0)  # an edit's EOM at 185 s
    assert before_liner(edited, ending) == edited


@pytest.mark.parametrize("kind", [EndingType.OTHER, EndingType.COLD, EndingType.UNMEASURED])
def test_before_liner_leaves_other_and_cold(kind: EndingType) -> None:
    ending = SongEnding(kind, None, 200_000)
    song = ruled_cues(STORED, ending, Follows.SONG)
    assert before_liner(song, ending) == song


# ---- values ---------------------------------------------------------------------------------


def test_version_and_enum_values() -> None:
    assert ENDING_ANALYSER_VERSION == 1
    assert [t.value for t in EndingType] == ["cold", "long_tail", "other", "unmeasured"]
    assert [f.value for f in Follows] == ["song", "liner"]


def test_song_ending_validation() -> None:
    with pytest.raises(InvalidStreamValueError):
        SongEnding(EndingType.LONG_TAIL, None, 200_000)
    with pytest.raises(InvalidStreamValueError):
        SongEnding(EndingType.LONG_TAIL, -1, 200_000)
    with pytest.raises(InvalidStreamValueError):
        SongEnding(EndingType.OTHER, 190_000, 200_000)


def test_loudness_tail_validation() -> None:
    with pytest.raises(InvalidStreamValueError):
        LoudnessTail(-1, FRAME, (0.0,))
    with pytest.raises(InvalidStreamValueError):
        LoudnessTail(0, 0, (0.0,))
    with pytest.raises(InvalidStreamValueError):
        LoudnessTail(0, FRAME, ())
    with pytest.raises(InvalidStreamValueError):
        LoudnessTail(0, FRAME, (0.0, float("nan")))


def test_playable_file_has_no_ending_by_default() -> None:
    file = PlayableFile(file_id=uuid.uuid4(), path="x.flac", duration_ms=1000, cues=None)
    assert file.ending is None
