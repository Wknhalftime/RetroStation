"""The rulings phase 3a PR 3 changes (SPEC "Rulings to touch", D4, the Definition of done).

PR 3 finishes 3a, so AUD-R032 (S1), AUD-R033 (S7) and AUD-R039 (S8) drop ``3a`` from their
check pending; their later phases stay. AUD-R040's ``3a`` went with PR 1. AUD-R076, the next free
branch ruling, records D4's amendment of C57 beside AUD-R039 (every matched line, not only the
949) and the rules PR 3 builds: ``choose_performance`` (the SPEC's D8 and D9), the
view's stored-performance step, the proposals and Credit picks' Apply and pick. Its check is PR
3's test files, all ``3a``, with nothing pending.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LEDGER = Path(__file__).resolve().parents[2] / "audit" / "rulings.jsonl"

PR3_CHECKS = [
    "tests/domain/test_choose_performance.py (3a)",
    "tests/integration/test_view_stored_performance_pg.py (3a)",
    "tests/integration/test_stored_performance_pg.py (3a)",
    "tests/tasks/test_credit_repoint_proposals.py (3a)",
    "tests/routers/test_credit_picks.py (3a)",
    "tests/scripts/test_apply_credit_picks.py (3a)",
]


def _ruling(ruling_id: str) -> dict[str, Any]:
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    [entry] = [e for e in map(json.loads, lines) if e["id"] == ruling_id]
    return dict(entry)


def _check(ruling_id: str) -> str:
    text = str(_ruling(ruling_id)["ruling"])
    return text[text.rfind("Check: ") :]


def test_r032_r033_and_r039_have_left_3a_behind() -> None:
    assert _check("AUD-R032").endswith("check pending: 3b.")
    assert _check("AUD-R033").endswith("check pending: 3b.")
    assert _check("AUD-R039").endswith("check pending: 4.4.")


def test_r076_amends_c57_beside_r039() -> None:
    ruling = _ruling("AUD-R076")

    assert ruling["status"] == "ACTIVE"
    assert {"AUD-R039", "AUD-R033", "AUD-R075"} <= set(ruling["refs"])
    assert any(text.startswith("AUD-R039") for text in ruling["supersedes"])
    assert "C57" in ruling["ruling"] and "every matched line" in ruling["ruling"]
    assert "choose_performance" in ruling["ruling"]
    assert "work's artist" in ruling["ruling"]  # D8


def test_r076_checks_pr3s_tests() -> None:
    check = _check("AUD-R076")

    assert check == f"Check: {', '.join(PR3_CHECKS)}."
