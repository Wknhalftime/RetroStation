"""Stable keys and UI formatters for why a match is in NEEDS_REVIEW state.

`ReasonCode` values are written to `broadcast_artists.reason_code` and
`track_identities.reason_code`. Do not rename values — they are persisted,
queried by telemetry, and asserted in characterization tests.

`reason_detail` strings are for curators only. They may include dynamic values
(score, gap). Keep them in this module so strategies never inline f-strings.
"""
from __future__ import annotations

import math
from enum import StrEnum


class ReasonCode(StrEnum):
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    AMBIGUOUS_GAP = "AMBIGUOUS_GAP"
    NO_CANDIDATES = "NO_CANDIDATES"
    NO_LOCAL_FILES = "NO_LOCAL_FILES"
    MB_SEARCH_INCONCLUSIVE = "MB_SEARCH_INCONCLUSIVE"
    MISSING_MATCH_RECORD = "MISSING_MATCH_RECORD"


def _round_half_up(value: float) -> int:
    # f"{x:.0f}" uses banker's rounding (72.5 -> 72). Curator-facing percentages
    # should round half away from zero. Scores/gaps are non-negative so
    # floor(x + 0.5) is sufficient.
    return math.floor(value + 0.5)


def format_low_confidence(score: float) -> str:
    return f"Score {_round_half_up(score)}% \u2014 below confidence threshold"


def format_ambiguous_gap(gap: float, threshold: float) -> str:
    return (
        f"Top candidates within {_round_half_up(gap)} points "
        f"(gap < {_round_half_up(threshold)} required)"
    )
