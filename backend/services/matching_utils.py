from __future__ import annotations

import re

from backend.services.normalization import normalize_title, strip_bracketed_groups


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


# Broadcast logs credit guests in shorthand after the title: "Smooth f/Rob
# Thomas", "The First Noel w/Faith Hill". normalize_title turns the slash
# into a space, after which "f rob thomas" can't be told from title words,
# so the credit comes off the raw title. f/ always means featuring.
_F_CREDIT = re.compile(
    r"\s*[\(\[]?\s*(?<![a-z0-9])f/\s*[^\)\]]+[\)\]]?\s*$",
    re.IGNORECASE,
)
# w/ is also "with" inside a title ("Killing Me Softly W/His Song"), and
# w/o and w/out mean "without".
_W_CREDIT = re.compile(
    r"\s*[\(\[]?\s*(?<![a-z0-9])w/(?!o\b|out\b)\s*[^\)\]]+[\)\]]?\s*$",
    re.IGNORECASE,
)


def _credit_stripped_forms(original_title: str) -> list[str]:
    """The raw title with its f/ credit stripped, then also without a trailing
    w/ credit when there is one. Never empty. See broadcast_title_variants."""
    primary = _F_CREDIT.sub("", original_title) or original_title
    forms = [primary]
    without_with = _W_CREDIT.sub("", primary)
    if without_with and without_with != primary:
        forms.append(without_with)
    return forms


def broadcast_title_variants(original_title: str) -> tuple[str, ...]:
    """Normalized forms of a raw broadcast title to score library titles against.

    The first has any f/ credit stripped; it is also the form to search
    MusicBrainz with. When the title ends in a w/ credit, a second form
    strips that too. w/ is as often part of the title itself, so callers
    keep whichever form scores better. A title that is nothing but a
    credit is kept whole.
    """
    return tuple(normalize_title(f) for f in _credit_stripped_forms(original_title))


def broadcast_title_core_variants(original_title: str) -> tuple[str, ...]:
    """The broadcast_title_variants again with their bracketed groups stripped.

    "(Stand By Me)" is an alternate title the library tag may not carry and
    "(Live)" a version it may; scoring the title without them recovers an
    otherwise exact match. Only forms that differ from the full ones are
    returned, so a plain title gives an empty tuple. Part numbers are never
    stripped (see strip_bracketed_groups).
    """
    full = broadcast_title_variants(original_title)
    forms: list[str] = []
    for raw in _credit_stripped_forms(original_title):
        core = normalize_title(strip_bracketed_groups(raw))
        if core and core not in full and core not in forms:
            forms.append(core)
    return tuple(forms)


def library_title_variants(
    track_title: str | None,
    normalized_title: str | None,
) -> tuple[str, ...]:
    """Normalized forms of a library file's title to score a broadcast title against.

    The first is the stored normalized_title (set by library_scan_service via
    normalize_title()), or normalize_title(track_title) for legacy rows that
    pre-date the backfill. Both sides of the comparison must live in that same
    canonical space: token_sort_ratio has no processor, so plain "Halo" vs
    "halo" would score 75. A bracketed group in the tag adds a form without
    it, mirroring broadcast_title_variants.
    """
    full = normalized_title or normalize_title(track_title or "")
    forms = [full]
    if track_title:
        core = normalize_title(strip_bracketed_groups(track_title))
        if core and core != full:
            forms.append(core)
    return tuple(forms)


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
    return len(name) >= max_len - TRUNCATION_TOLERANCE_CHARS and name[-1].isalnum()
