"""The rulings phase 2a PR 2 changes (SPEC S8, S10, D5, the ruling-markers line).

AUD-R032's ``2a`` marker goes with S10's C43, and its later phases stay pending. AUD-R041's goes
in PR 3 (``test_rulings_phase_2a_merge.py``). AUD-R073 amends AUD-R072: a further credit promotes
only on exactly this MBID (D5), and D8's gap is accepted, for phase 5's page.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LEDGER = Path(__file__).resolve().parents[2] / "audit" / "rulings.jsonl"


def _ruling(ruling_id: str) -> dict[str, Any]:
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    [entry] = [e for e in map(json.loads, lines) if e["id"] == ruling_id]
    return dict(entry)


def _check(ruling_id: str) -> str:
    text = str(_ruling(ruling_id)["ruling"])
    return text[text.rfind("Check: ") :]


def test_r032_has_left_2a_behind() -> None:
    assert _check("AUD-R032").endswith("check pending: 3b.")


def test_r073_amends_r072() -> None:
    amending = _ruling("AUD-R073")

    assert amending["status"] == "ACTIVE"
    assert any(text.startswith("AUD-R072") for text in amending["supersedes"])
    assert "tests/services/test_enrichment_upserts_all_credits.py (2a)" in amending["ruling"]
    assert "further credit" in amending["ruling"]
    assert "DUPLICATE" in amending["ruling"]  # D8: the gap it accepts
