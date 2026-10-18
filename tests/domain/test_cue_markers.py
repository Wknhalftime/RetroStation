"""Cue editor PR 1, Task 1: the four markers, their engine mapping and the edit rules.

Requirements (docs/superpowers/plans/2026-10-17-cue-editor.md):
R1 ordering of the four markers; R2 marker-to-engine mapping with fade-in always 0;
R3 opening values (autocue's, or what the radio plays with no cues today);
R5 the gain an edit plays at. Spec rulings C1 (fade shape), C2 (opening values),
C3 (no fade-in), C5 (songs without a real analysis).
"""

from __future__ import annotations

import pytest

from backend.domain.streaming import (
    NO_CUES_GAIN_DB,
    CueAudioUnreadableError,
    CueEditError,
    CueEditRejectedError,
    CueFileNotEditableError,
    CueFileNotFoundError,
    CueMarkers,
    CuePoints,
    InvalidStreamValueError,
    StreamingError,
    StreamTiming,
    edited_cues,
    no_cue_points,
)

# Heretic Anthem's autocue row on dev, 2026-10-17.
HERETIC = CuePoints(
    cue_in_ms=0,
    cue_out_ms=247_100,
    fade_in_ms=0,
    fade_out_ms=6_000,
    start_next_ms=6_000,
    gain_db=-11.63,
)


def test_from_points_puts_eom_and_fade_at_autocue_fade_start():
    """R3, C2: a real analysis opens with EOM and Fade-out at autocue's fade start."""
    assert CueMarkers.from_points(HERETIC) == CueMarkers(0, 241_100, 241_100, 247_100)


def test_from_points_clamps_a_fade_longer_than_the_span_to_start():
    """R3 (plan note 5): a fade longer than the span puts Fade-out at Start, not before."""
    points = CuePoints(
        cue_in_ms=1_000,
        cue_out_ms=3_000,
        fade_in_ms=0,
        fade_out_ms=4_000,
        start_next_ms=0,
        gain_db=0.0,
    )

    assert CueMarkers.from_points(points) == CueMarkers(1_000, 3_000, 1_000, 3_000)


def test_to_points_maps_markers_to_engine_values():
    """R2, C1: Start, Stop, Stop - Fade-out and Stop - EOM become the engine's four values."""
    devil = CueMarkers(start_ms=0, eom_ms=271_000, fade_at_ms=270_500, stop_ms=271_800)

    assert devil.to_points(-10.958) == CuePoints(
        cue_in_ms=0,
        cue_out_ms=271_800,
        fade_in_ms=0,
        fade_out_ms=1_300,
        start_next_ms=800,
        gain_db=-10.958,
    )


def test_to_points_never_has_a_fade_in():
    """R2, C3: a manual edit stores no fade-in, wherever Start is."""
    assert CueMarkers(9_000, 241_100, 240_000, 247_100).to_points(0.0).fade_in_ms == 0


def test_fade_at_equal_to_stop_is_a_hard_stop():
    """R2, C1: Fade-out at Stop means a fade of length zero."""
    assert CueMarkers(0, 5_000, 5_000, 5_000).to_points(0.0).fade_out_ms == 0


def test_round_trip_from_points_to_points():
    """R2, R3: opening a measured song and saving it unchanged reproduces autocue's points."""
    assert CueMarkers.from_points(HERETIC).to_points(HERETIC.gain_db) == HERETIC


@pytest.mark.parametrize(
    ("start", "eom", "fade_at", "stop"),
    [
        (-1, 10, 5, 20),  # start < 0
        (10, 10, 15, 20),  # eom must be > start
        (0, 21, 5, 20),  # eom > stop
        (5, 10, 4, 20),  # fade_at < start
        (0, 10, 21, 20),  # fade_at > stop
        (20, 20, 20, 20),  # empty span
    ],
)
def test_invalid_orderings_raise(start: int, eom: int, fade_at: int, stop: int):
    """R1: 0 <= start < eom <= stop and start <= fade_at <= stop; anything else is refused."""
    with pytest.raises(InvalidStreamValueError):
        CueMarkers(start, eom, fade_at, stop)


def test_eom_may_sit_either_side_of_fade_at():
    """R1: EOM and Fade-out may be in either order relative to each other."""
    CueMarkers(0, 9_000, 8_000, 10_000)  # EOM after Fade-out: must not raise
    CueMarkers(0, 8_000, 9_000, 10_000)  # EOM before Fade-out: must not raise


def test_no_cue_points_are_d23_with_no_fade_in():
    """R3, C5, C3: with no cues the radio plays Start 0, Stop = duration, a 4 s fade, gain 0."""
    assert no_cue_points(200_000, StreamTiming()) == CuePoints(
        cue_in_ms=0,
        cue_out_ms=200_000,
        fade_in_ms=0,
        fade_out_ms=4_000,
        start_next_ms=0,
        gain_db=0.0,
    )


def test_no_cue_markers_open_at_file_end_with_4s_fade():
    """R3, C5: an unanalysed song opens with Stop = EOM = duration and Fade-out 4 s before."""
    markers = CueMarkers.from_points(no_cue_points(200_000, StreamTiming()))

    assert markers == CueMarkers(0, 200_000, 196_000, 200_000)


def test_edited_cues_take_the_markers_not_the_stored_positions():
    """R2, R5: the edit supplies every point; the stored row supplies only the gain."""
    points = edited_cues(CueMarkers(2_000, 9_000, 8_000, 10_000), HERETIC)

    assert points == CuePoints(
        cue_in_ms=2_000,
        cue_out_ms=10_000,
        fade_in_ms=0,
        fade_out_ms=2_000,
        start_next_ms=1_000,
        gain_db=-11.63,
    )


def test_edited_cues_take_stored_gain():
    """R5, plan note 6: an edit never changes loudness; the autocue row's gain stays."""
    assert edited_cues(CueMarkers(0, 9_000, 9_000, 10_000), HERETIC).gain_db == -11.63


def test_edited_cues_without_stored_row_use_no_cues_gain():
    """R5, plan note 6: with no autocue row the edit plays at the no-cues gain (D23)."""
    markers = CueMarkers(0, 9_000, 9_000, 10_000)

    assert edited_cues(markers, None).gain_db == NO_CUES_GAIN_DB == 0.0


@pytest.mark.parametrize(
    "error",
    [
        CueFileNotFoundError,
        CueFileNotEditableError,
        CueEditRejectedError,
        CueAudioUnreadableError,
    ],
)
def test_cue_edit_errors_share_one_base(error: type[Exception]):
    """Task 1 Interfaces: every editor refusal is a CueEditError, itself a StreamingError."""
    assert issubclass(error, CueEditError)
    assert issubclass(CueEditError, StreamingError)
