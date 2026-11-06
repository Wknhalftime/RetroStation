"""The rulings phase 2a PR 1 changes (SPEC S12, D1, D3, D6, D7).

AUD-R071 amends AUD-R042: the MusicBrainz limiter is AUD-R102's machine-wide clock (D1), so
AUD-R042 no longer speaks of a database row, its Check names ``tests/services/test_mb_clock.py``,
and 2a has left its pending list. AUD-R072 amends AUD-R041 (S5, D3, D6, D7). AUD-R041's own 2a
marker stays until PR 3, whose test files it names, so nothing here pins it.
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


def test_r071_amends_r042_and_r042_says_so() -> None:
    amending = _ruling("AUD-R071")
    amended = _ruling("AUD-R042")

    assert amending["status"] == "ACTIVE"
    assert any(text.startswith("AUD-R042") for text in amending["supersedes"])
    assert "AUD-R102" in amending["refs"]
    assert "database row" not in amended["ruling"]
    assert "AUD-R102" in amended["ruling"]
    check = amended["ruling"][amended["ruling"].rfind("Check: ") :]
    assert check.startswith("Check: tests/services/test_mb_clock.py (2a), ")
    assert check.endswith("check pending: 4.1.")


def test_r072_amends_r041() -> None:
    amending = _ruling("AUD-R072")

    assert amending["status"] == "ACTIVE"
    assert any(text.startswith("AUD-R041") for text in amending["supersedes"])
    assert "tests/integration/test_artist_name_index_pg.py (2a)" in amending["ruling"]
    assert "suggestion" in amending["ruling"]  # D7: a match on a name the log name rejected
