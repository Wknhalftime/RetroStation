"""The ruling phase 4.1 PR 2b adds (MusicBrainz cache fill: the request budget; SPEC S8–S11, E6;
D9, D10, D11).

AUD-R093 settles D9's blocker before the first live fill: each fill call makes one request
through a one-attempt view of the client, the fill waits 10 s after a refusal, a run hands on no
batch after 120 minutes, and its rows report failures, requests, minutes and how it ended. It
refs AUD-R091 (the fill) and AUD-R102 (the one door and its clock) and supersedes nothing:
AUD-R091's cut-off, caps and batches all stand, and ``_fetch``'s 3 tries stay for every other
caller. Its rationale keeps part B's measured facts, among them that a refused query stays
refused a while, which is why a refused key waits for the next run instead of a re-ask.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LEDGER = Path(__file__).resolve().parents[2] / "audit" / "rulings.jsonl"

CHECK = (
    "Check: tests/tasks/test_cache_fill_budget.py (4.1), "
    "tests/services/test_mb_client_attempts.py (4.1), "
    "tests/services/test_cache_fill_budget_limits.py (4.1)."
)


def _ruling(ruling_id: str) -> dict[str, Any]:
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    [entry] = [e for e in map(json.loads, lines) if e["id"] == ruling_id]
    return dict(entry)


def test_r093_settles_the_fills_request_budget() -> None:
    ruling = _ruling("AUD-R093")

    assert ruling["status"] == "ACTIVE"
    assert {"AUD-R091", "AUD-R102"} <= set(ruling["refs"])
    assert ruling["supersedes"] == []
    assert ruling["ruling"].endswith(CHECK)
    for words in (
        "with_attempts(1)",
        "10 s",
        "110 s",
        "120 minutes",
        "started_at",
        "requests_sent",
        "mb_fill_calls_failed",
        "failed_calls",
        "ended",
        "3 tries",
    ):
        assert words in ruling["ruling"], words
    for words in ("D9", "D10", "D11", "4,218", "77–90%", "50%"):
        assert words in ruling["rationale"], words


def test_r091_still_stands() -> None:
    ruling = _ruling("AUD-R091")

    assert ruling["status"] == "ACTIVE"
    assert "Five transient failures in a row" in ruling["ruling"]
