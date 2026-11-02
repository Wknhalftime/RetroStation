"""Acceptance: the product's V11.1 title score is the frozen V11.1 score (SPEC 2026-11-01, S1).

V11.1 was tuned on Lance's 1,712 song decisions and frozen with checksums in a folder that stays
on his machine (docs/superpowers/specs/2026-11-01-title-scoring-v11-frozen). His later decisions
grade it blind, so the scorer that ships must give exactly the frozen scores, or the grade says
nothing about it. tests/fixtures/title_scoring_v11.json holds every decided title pair and the 43
sentinels with the frozen score, generated once from the verified folder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.services.title_scoring import v11_title_score

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "title_scoring_v11.json"


def test_matches_frozen_reference() -> None:
    pairs = json.loads(_FIXTURE.read_text(encoding="utf-8"))["pairs"]
    assert len(pairs) == 1736

    wrong = [
        (p["log_title"], p["file_title"], p["score"], got)
        for p in pairs
        if abs((got := v11_title_score(p["log_title"], p["file_title"])) - p["score"]) > 0.001
    ]

    assert not wrong, f"{len(wrong)} pairs differ from frozen V11.1, e.g. {wrong[:10]}"


@pytest.mark.parametrize(
    ("log_title", "file_title"),
    [("...", "Hello"), ("", ""), ("Hello", ""), ("...", "")],
    ids=["log_only_dots", "both_empty", "file_empty", "dots_and_empty"],
)
def test_empty_title_scores_zero(log_title: str, file_title: str) -> None:
    """E1: a title that normalizes to nothing scores 0 and raises nothing."""
    assert v11_title_score(log_title, file_title) == 0.0
