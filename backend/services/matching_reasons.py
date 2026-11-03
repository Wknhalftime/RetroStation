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
    "format_answer_suggested",
    "format_deferred_retry",
    "format_low_confidence",
    "format_mb_unavailable",
    "format_title_scores_disagree",
    "suggestion_reason",
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


def format_answer_suggested(score: float) -> str:
    # The matcher's own answer under 95 (phase 1c S4, C35): what the name's suggestions are is
    # said when the status is written from its links (domain/log_name_status.py).
    return f"Best score {_round_half_up(score)}% — suggested, not linked"


def suggestion_reason(origin: str, tier: str | None, score: int) -> str:
    # Why the Resolution Center shows a suggested link (phase 1c S8), from the link's origin
    # and the tier of its evidence: a fuzzy link migration 0045 demoted (M22), or the
    # matcher's answer under 95 (C35).
    if origin == "legacy":
        return "Was linked automatically by a close name; now a suggestion to confirm"
    if tier == "musicbrainz_api":
        return "Found by a MusicBrainz search for the name"
    if tier == "normalization" and score >= 100:
        return "Same name as this artist, and as others: pick the right one"
    if tier == "normalization":
        return "Close name in the catalog"
    return "Suggested automatically"


def format_deferred_retry() -> str:
    # Path-agnostic on purpose: DEFERRED_RETRY fires both when a non-truncated
    # name skips MB entirely AND when a truncated name reaches MB but gets no
    # candidates. The cascade through bulk_defer_by_artist propagates this
    # same reason to child identities. "Unresolved across all matching tiers"
    # covers both without misleading curators about which path was taken.
    return "Unresolved across all matching tiers — deferred for retry on next playlist"


def format_mb_unavailable() -> str:
    # Song matching's Step B could not reach MusicBrainz (AUD-R028, D16). Every re-check rewinds
    # a deferred song, whatever its artist.
    return "MusicBrainz unavailable — deferred for retry at the next re-check"


def format_title_scores_disagree(today_score: float, v11_score: float) -> str:
    # Trial mode (AUD-R100): the card shows V11.1's pick; a song V11.1 alone hides is hidden
    # with this detail (AUD-R101).
    return (
        f"Title scorers disagree — today's {_round_half_up(today_score)}%, "
        f"V11.1 {_round_half_up(v11_score)}%"
    )


def format_link_rejected(artist_name: str) -> str:
    # The matcher's match is held back (phase 1b S4, C19): the curator rejected this link, so
    # the name waits in review for them instead of being matched again.
    return f"Matched {artist_name}, a link you rejected — waiting for your decision"
