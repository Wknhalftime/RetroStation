"""The ruling phase 4.1 PR 2 adds, and the marker it drops (MusicBrainz cache fill; SPEC S3, S4,
S7, E1, E2, E4, E5; D3, D4, D6).

AUD-R091 settles PR 2's part of AUD-R042: the fill itself. That covers its tiers and the names it
searches, its batches, caps and cut-off, its per-tier re-checks, the alias step, and 0058's
confirmations queue. It amends nothing AUD-R042 says, so it supersedes nothing, and its check
files all arrive in PR 2. AUD-R042's own 4.1 marker drops here: both of its 4.1 check files exist
now (``test_rulings_phase_2a.py``, relocked with this PR, reads that nothing is pending).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LEDGER = Path(__file__).resolve().parents[2] / "audit" / "rulings.jsonl"

CHECK = (
    "Check: tests/tasks/test_cache_fill.py (4.1), "
    "tests/integration/test_cache_fill_tiers_pg.py (4.1), "
    "tests/services/test_cache_fill_words.py (4.1), "
    "tests/services/test_cache_fill_limits.py (4.1), "
    "tests/tasks/test_rematch_names_task.py (4.1), "
    "tests/integration/test_mb_cache_fill_confirmations_pg.py (4.1)."
)


def _ruling(ruling_id: str) -> dict[str, Any]:
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    [entry] = [e for e in map(json.loads, lines) if e["id"] == ruling_id]
    return dict(entry)


def test_r091_settles_pr2_of_r042() -> None:
    ruling = _ruling("AUD-R091")

    assert ruling["status"] == "ACTIVE"
    assert {"AUD-R042", "AUD-R090", "AUD-R014", "AUD-R029"} <= set(ruling["refs"])
    assert ruling["supersedes"] == []
    assert ruling["ruling"].endswith(CHECK)
    for words in (
        "payoff",
        "C68",
        "C18",
        "confirmations",
        "tail",
        "needs_review",
        "auto_rejected",
        "mb_cache_fill_task",
        "-10",
        "110",
        "rematch_names_task",
        "artist-aliases:",
        "musicbrainz_alias",
        "mb_cache_fill_confirmations",
        "title_only",
    ):
        assert words in ruling["ruling"], words
    for decision in ("D3", "D4", "D6"):
        assert decision in ruling["rationale"], decision


def test_r042_has_nothing_pending() -> None:
    ruling = _ruling("AUD-R042")["ruling"]
    check = ruling[ruling.rfind("Check: ") :]

    assert "pending" not in check
    assert check.endswith("tests/tasks/test_cache_fill.py (4.1).")
