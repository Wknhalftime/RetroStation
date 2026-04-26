"""UI formatters for why a match is in NEEDS_REVIEW state.

`ReasonCode` lives in `backend.domain.enums` (domain → services would be a
layering violation). It is re-exported here so existing imports keep working;
new code should import it from `backend.domain.enums` directly.

`reason_detail` strings are for curators only. They may include dynamic values
(score, gap). Keep them in this module so strategies never inline f-strings.
"""
from __future__ import annotations

import math

from backend.domain.enums import ReasonCode

__all__ = [
    "ReasonCode",
    "format_ambiguous_gap",
    "format_deferred_retry",
    "format_low_confidence",
]


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


def format_deferred_retry() -> str:
    # Path-agnostic on purpose: DEFERRED_RETRY fires both when a non-truncated
    # name skips MB entirely AND when a truncated name reaches MB but gets no
    # candidates. The cascade through bulk_defer_by_artist propagates this
    # same reason to child identities. "Unresolved across all matching tiers"
    # covers both without misleading curators about which path was taken.
    return (
        "Unresolved across all matching tiers — "
        "deferred for retry on next playlist"
    )
