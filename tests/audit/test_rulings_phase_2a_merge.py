"""The ruling phase 2a PR 3 changes (SPEC S11, the ruling-markers line).

AUD-R041's ``2a`` marker goes in PR 3, with the last of its 2a check files,
``tests/integration/test_artist_merge_pg.py``. S11 builds what AUD-R041 already says (M33: merging
is on request from the artist page, and Unmerge undoes it), so PR 3 writes no new ruling. The
pending list is read, not matched, so a later phase's marker going needs no change here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.audit.test_rulings_ledger import parse_check

LEDGER = Path(__file__).resolve().parents[2] / "audit" / "rulings.jsonl"


def _ruling(ruling_id: str) -> dict[str, Any]:
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    [entry] = [e for e in map(json.loads, lines) if e["id"] == ruling_id]
    return dict(entry)


def test_r041_has_left_2a_behind() -> None:
    check = parse_check(str(_ruling("AUD-R041")["ruling"]))

    assert check is not None
    assert "2a" not in check.pending
    assert {path for path, phase in check.files if phase == "2a"} == {
        "tests/services/test_pair_credit.py",
        "tests/integration/test_artist_merge_pg.py",
    }
