"""Reading Liquidsoap's autocue output as cue points.

Spec: Cue pre-computation ("autocue.internal, lufs_target -18, amplify_behavior "keep""); Data
(start_next_ms "counted back from cue_out"; gain_db "gain to reach -18 LUFS"); house rule:
validate on load and name the field. The RECORDED rows are real autocue.internal output
(Liquidsoap 2.4.5, PR A spike 2026-09-27, out/r2/autocue_cache.jsonl), kept as the contract
the parser is held to, as MusicBrainz responses are held to their cassettes. The mapping
start_next_ms = liq_cross_end_duration is ruling D54.
"""

from __future__ import annotations

import pytest

from backend.domain.streaming import CuePoints
from backend.services.streaming.autocue import Unusable, autocue_points

REQUIRED = (
    "liq_cue_in",
    "liq_cue_out",
    "liq_fade_in",
    "liq_fade_out",
    "liq_cross_end_duration",
    "liq_amplify",
)

VAN_HALEN: dict[str, str] = {
    "liq_autocue": "internal",
    "liq_amplify": "3.645 dB",
    "liq_cue_in": "0.0",
    "liq_cue_out": "67.493",
    "liq_cross_start_duration": "0.0",
    "liq_cross_max_start_duration": "63.3",
    "liq_cross_end_duration": "4.193",
    "liq_fade_in": "0.0",
    "liq_fade_out": "4.193",
    "liq_fade_out_start_next": "0.0",
    "liq_fade_out_delay": "0.0",
}
VAN_HALEN_POINTS = CuePoints(
    cue_in_ms=0,
    cue_out_ms=67_493,
    fade_in_ms=0,
    fade_out_ms=4_193,
    start_next_ms=4_193,
    gain_db=3.645,
)
BEHEMOTH: dict[str, str] = {
    "liq_autocue": "internal",
    "liq_amplify": "-11.907 dB",
    "liq_cue_in": "0.399977324263",
    "liq_cue_out": "181.199977324",
    "liq_cross_start_duration": "0.2",
    "liq_cross_max_start_duration": "178.700022676",
    "liq_cross_end_duration": "2.099977324",
    "liq_fade_in": "0.2",
    "liq_fade_out": "2.099977324",
    "liq_fade_out_start_next": "0.0",
    "liq_fade_out_delay": "0.0",
}
HAGAR: dict[str, str] = {
    "liq_autocue": "internal",
    "liq_amplify": "-4.079 dB",
    "liq_cue_in": "0.0",
    "liq_cue_out": "201.4",
    "liq_cross_start_duration": "0.0",
    "liq_cross_max_start_duration": "200.6",
    "liq_cross_end_duration": "0.8",
    "liq_fade_in": "0.0",
    "liq_fade_out": "0.8",
    "liq_fade_out_start_next": "0.0",
    "liq_fade_out_delay": "0.0",
}
THIRD_EYE: dict[str, str] = {  # 827 s: analysed only because timeout is 15 s (spike check 5)
    "liq_autocue": "internal",
    "liq_amplify": "-7.572 dB",
    "liq_cue_in": "0.0",
    "liq_cue_out": "825.2",
    "liq_cross_start_duration": "0.0",
    "liq_cross_max_start_duration": "820.4",
    "liq_cross_end_duration": "4.8",
    "liq_fade_in": "0.0",
    "liq_fade_out": "4.8",
    "liq_fade_out_start_next": "0.0",
    "liq_fade_out_delay": "0.0",
}
RECORDED = {
    "Van Halen - 1984": (VAN_HALEN, VAN_HALEN_POINTS),
    "Behemoth - Slaves Shall Serve": (
        BEHEMOTH,
        CuePoints(
            cue_in_ms=400,
            cue_out_ms=181_200,
            fade_in_ms=200,
            fade_out_ms=2_100,
            start_next_ms=2_100,
            gain_db=-11.907,
        ),
    ),
    "Sammy Hagar - The Girl Gets Around": (
        HAGAR,
        CuePoints(
            cue_in_ms=0,
            cue_out_ms=201_400,
            fade_in_ms=0,
            fade_out_ms=800,
            start_next_ms=800,
            gain_db=-4.079,
        ),
    ),
    "Tool - Third Eye (over 700 s)": (
        THIRD_EYE,
        CuePoints(
            cue_in_ms=0,
            cue_out_ms=825_200,
            fade_in_ms=0,
            fade_out_ms=4_800,
            start_next_ms=4_800,
            gain_db=-7.572,
        ),
    ),
}


@pytest.mark.parametrize(("metadata", "expected"), RECORDED.values(), ids=RECORDED.keys())
def test_recorded_autocue_output_maps_to_cue_points(
    metadata: dict[str, str], expected: CuePoints
) -> None:
    """Cue pre-computation: autocue.internal's values become the stored cue points; start-next
    counts back from cue-out (Data); the gain is autocue's gain to -18 LUFS."""
    assert autocue_points(metadata) == expected


def test_seconds_become_the_nearest_millisecond() -> None:
    """Cue points are whole milliseconds (CuePoints); autocue reports float seconds."""
    points = autocue_points({**VAN_HALEN, "liq_cue_in": "0.0004999", "liq_cue_out": "67.4935001"})
    assert isinstance(points, CuePoints)
    assert (points.cue_in_ms, points.cue_out_ms) == (0, 67_494)


def test_autocue_declining_gives_no_cue_points() -> None:
    """Spike check 5: a file autocue declines (too long for the timeout, unreadable) returns
    no metadata; that is a failed analysis, not an error."""
    assert isinstance(autocue_points({}), Unusable)


@pytest.mark.parametrize("key", REQUIRED)
def test_a_missing_key_is_named(key: str) -> None:
    """House rule: validate on load and name the field."""
    result = autocue_points({k: v for k, v in VAN_HALEN.items() if k != key})
    assert isinstance(result, Unusable)
    assert key in result.reason


@pytest.mark.parametrize("value", ["abc", "", "nan", "inf"])
def test_a_value_that_is_not_seconds_is_named(value: str) -> None:
    """House rule: validate on load and name the field (finite seconds only)."""
    result = autocue_points({**VAN_HALEN, "liq_cue_out": value})
    assert isinstance(result, Unusable)
    assert "liq_cue_out" in result.reason


@pytest.mark.parametrize("value", ["3.6 dBx", "loud dB", "nan dB", "inf dB"])
def test_a_gain_that_is_not_in_db_is_named(value: str) -> None:
    """The contract's gain format is "-6.2 dB"; anything else is named, not guessed."""
    result = autocue_points({**VAN_HALEN, "liq_amplify": value})
    assert isinstance(result, Unusable)
    assert "liq_amplify" in result.reason


@pytest.mark.parametrize(
    "change",
    [
        {"liq_cross_end_duration": "67.493"},
        {"liq_cue_out": "0.0"},
        {"liq_cue_in": "-0.5"},
    ],
    ids=["start-next as long as the span", "cue-out at cue-in", "negative cue-in"],
)
def test_cue_points_that_cannot_play_are_unusable(change: dict[str, str]) -> None:
    """CuePoints' invariants hold for stored rows; a violation is a failed analysis (and a
    fallback row), never an exception out of the parser."""
    assert isinstance(autocue_points({**VAN_HALEN, **change}), Unusable)
