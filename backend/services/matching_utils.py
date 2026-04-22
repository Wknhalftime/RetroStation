from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.domain.matching import MappingRule
    from backend.repositories.mapping_rules import MappingRuleRepository


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


def find_rule_match(
    normalized_value: str,
    rules_repo: MappingRuleRepository,
) -> MappingRule | None:
    """Return the first rule whose pattern matches normalized_value, or None."""
    for rule in rules_repo.list_ordered():
        if rule_matches(rule.source_pattern, normalized_value):
            return rule
    return None


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
