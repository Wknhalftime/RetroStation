"""The ruling phase 2a PR 3 changes (SPEC S11, the ruling-markers line).

AUD-R041's ``2a`` marker goes in PR 3, with the last of its 2a check files,
``tests/integration/test_artist_merge_pg.py``; phase 6 stays pending. S11 builds what AUD-R041
already says (M33: merging is on request from the artist page, and Unmerge undoes it), so PR 3
writes no new ruling.
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


def test_r041_has_left_2a_behind() -> None:
    check = _check("AUD-R041")

    assert check.startswith(
        "Check: tests/services/test_pair_credit.py (2a), "
        "tests/integration/test_artist_merge_pg.py (2a), "
    )
    assert check.endswith("; check pending: 6.")
