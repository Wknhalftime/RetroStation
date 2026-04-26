from __future__ import annotations

import re


def rule_matches(source_pattern: str, normalized_value: str) -> bool:
    """Check if a global mapping rule's source_pattern matches the normalized value.

    Performs an exact string comparison first, then falls back to a full regex
    match. Returns False if the pattern is an invalid regular expression.

    Args:
        source_pattern: The rule's source pattern (literal string or regex).
        normalized_value: The normalized artist name or identity signature.

    Returns:
        True if the pattern matches the value, False otherwise.
    """
    if source_pattern == normalized_value:
        return True
    try:
        return bool(re.fullmatch(source_pattern, normalized_value))
    except re.error:
        return False


_STRIP_SUFFIXES = re.compile(
    r"\s*[\(\[](live|remix|edit|radio edit|acoustic|extended|extended mix|"
    r"remaster(?:ed)?|remastered version|original mix|club mix|instrumental|"
    r"reprise|single version|album version)\s*[\)\]]",
    re.IGNORECASE,
)

_STRIP_FEAT = re.compile(
    r"\s*[\(\[]?(?:feat(?:uring)?|ft)\.?\s+[^\)\]]+[\)\]]?",
    re.IGNORECASE,
)


def normalize_title_for_scoring(title: str) -> str:
    """Strip broadcast/library title to canonical core before fuzzy scoring.

    Removes (Live), (Remix), (Edit), (Radio Edit), (Acoustic), feat. clauses,
    and common variant suffixes. Applied to BOTH sides of every fuzzy comparison.
    Result: "Song Title (Live)" vs "Song Title" -> 100, not 75.
    """
    t = _STRIP_FEAT.sub("", title)
    t = _STRIP_SUFFIXES.sub("", t)
    return t.strip()


TRUNCATION_TOLERANCE_CHARS: int = 2
"""Characters below ``max_len`` still considered "at the limit".

Absorbs trailing-space trimming variations across broadcast feeds. A name of
length ``max_len - TRUNCATION_TOLERANCE_CHARS`` ending in an alphanumeric
character is treated as likely truncated.
"""


def is_likely_truncated(name: str, max_len: int) -> bool:
    """True when ``name`` appears cut off by a fixed-width broadcast field.

    Heuristic: at or near the field limit AND the final character is alphanumeric.
    Clean short names typically end with punctuation or a clear word boundary;
    a name that maxes out the field and ends mid-word is the diagnostic signal.

    Empty input returns False — a missing name is a different problem.
    """
    if not name:
        return False
    return (
        len(name) >= max_len - TRUNCATION_TOLERANCE_CHARS
        and name[-1].isalnum()
    )
