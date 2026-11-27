"""The ruling phase 4.2 PR 1 adds, and the markers it drops (evidence linking: decide and the
harness; SPEC S1, S2, S3, S6, S8, E3, E5; D1, D3; Lance, 2026-11-27: "All as recommended").

AUD-R095 settles PR 1: ``decide()``'s rules (C1, C9, C10, C67, M9, M10, M14, and D1's votes),
the evidence sources (C2, C8, C24, C32), the thresholds built once (C25, C48) and the harness
with C12's gate. AUD-R036's check is now its one file, so its "check pending: 4.2" drops.
AUD-R043's stays until PR 2 adds ``test_linker_gate.py``; AUD-R037's is 4.4's.

Whether AUD-R095 or PR 2's AUD-R096 supersedes AUD-R043's "a sample of 200" in part (D3) is the
lock request's first ask; that is the one case below that turns on it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LEDGER = Path(__file__).resolve().parents[2] / "audit" / "rulings.jsonl"

CHECK = (
    "Check: tests/domain/test_decide.py (4.2), tests/services/test_link_evidence_sources.py"
    " (4.2), tests/services/test_thresholds_built_once.py (4.2),"
    " tests/scripts/test_link_harness.py (4.2)."
)


def _ruling(ruling_id: str) -> dict[str, Any]:
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    [entry] = [e for e in map(json.loads, lines) if e["id"] == ruling_id]
    return dict(entry)


def test_r095_settles_decide_the_sources_the_thresholds_and_the_harness() -> None:
    ruling = _ruling("AUD-R095")

    assert ruling["status"] == "ACTIVE"
    assert {"AUD-R036", "AUD-R037", "AUD-R043", "AUD-R022", "AUD-R091"} <= set(ruling["refs"])
    assert ruling["ruling"].endswith(CHECK)
    for words in (
        "decide(",
        "two independent signals",
        "covering hits",
        "title_lead",
        "no score is read",
        "common",
        "owned-song anchor",
        "near miss",
        "manual link",
        "WAITING_FOR_MUSICBRAINZ",
        "build_thresholds",
        "poe link-harness",
        "at most 5 wrong",
        "70%",
    ):
        assert words in ruling["ruling"], words
    for words in ("D1", "D3", "All as recommended", "2026-11-27"):
        assert words in ruling["rationale"], words


def test_r095_supersedes_r043_in_part_for_sweep_made_links() -> None:
    """D3: a wrong sweep-made link counts toward the 5, beside the 200 (the first ask)."""
    [entry] = _ruling("AUD-R095")["supersedes"]

    assert entry.startswith("AUD-R043 (in part): 'on a sample of 200 automatic links: at most 5")
    assert "sweep" in entry
    assert "D3" in entry


def test_r036_check_is_no_longer_pending() -> None:
    assert _ruling("AUD-R036")["ruling"].endswith("Check: tests/domain/test_decide.py (4.2).")


def test_r043_waits_for_pr2_and_r037_for_4_4() -> None:
    assert _ruling("AUD-R043")["ruling"].endswith("; check pending: 4.2.")
    assert _ruling("AUD-R037")["ruling"].endswith("; check pending: 4.4.")
