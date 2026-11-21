"""Phase 3b PR 5b's rulings and Retired rows (work merges phase 3b PR 5b, SPEC S7, S9, D34, D44;
C70; AUD-R082 to AUD-R088).

Each name 5b retires has a Retired row of phase 3b citing its own ruling, so
``tests/test_retired_names.py`` keeps it retired:

- AUD-R082: ``works.artist_id`` (its index ``idx_works_artist``);
- AUD-R083: ``matches.work_id`` (``idx_matches_work_id``, ``WorkReference.MATCH``,
  ``SongMatchService.follow_file`` and ``.release_work``, the repository's ``move_to_work`` and
  ``release_work_matches``);
- AUD-R084: ``WorkRepository.get_by_artist``, and not ``get_by_artist_mbid``, which stays;
- AUD-R085 and AUD-R086: D34's ``delete_overrides_of_work`` and ``delete_recordings_of_work``;
- AUD-R087: ``scripts/dry_run_untagged_recordings.py`` (D44), which is gone.

AUD-R088 holds 5b's decisions. All seven are in the ledger, active.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_retired_names import RetiredRow, retired_rows

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "audit" / "rulings.jsonl"

RETIRED = [
    ("AUD-R082", "idx_works_artist"),
    ("AUD-R083", "idx_matches_work_id"),
    ("AUD-R083", "WorkReference.MATCH"),
    ("AUD-R083", "follow_file("),
    ("AUD-R083", "release_work("),
    ("AUD-R083", "matches.move_to_work("),
    ("AUD-R083", "release_work_matches("),
    ("AUD-R084", "get_by_artist("),
    ("AUD-R085", "delete_overrides_of_work("),
    ("AUD-R086", "delete_recordings_of_work("),
    ("AUD-R087", "dry_run_untagged_recordings"),
]
KEPT = ["get_by_artist_mbid(", "release_line_pins(", "format_overrides.move_to_work("]


def _rows_of(ruling: str) -> list[RetiredRow]:
    return [row for row in retired_rows() if row.ruling == ruling]


@pytest.mark.parametrize(("ruling", "name"), RETIRED)
def test_each_retired_name_has_a_phase_3b_row_citing_its_ruling(ruling: str, name: str) -> None:
    rows = _rows_of(ruling)
    assert rows, f"no Retired row cites {ruling}"
    assert all(row.phase == "3b" for row in rows), [row.phase for row in rows]
    assert any(re.search(p, name) for row in rows for p in row.patterns), (ruling, name)


@pytest.mark.parametrize("name", KEPT)
def test_no_5b_row_reaches_a_name_5b_keeps(name: str) -> None:
    for ruling in sorted({r for r, _ in RETIRED}):
        for row in _rows_of(ruling):
            assert not any(re.search(p, name) for p in row.patterns), (ruling, name)


def test_the_retired_script_is_gone() -> None:
    assert not (ROOT / "scripts" / "dry_run_untagged_recordings.py").exists()


def test_the_seven_rulings_are_in_the_ledger_and_active() -> None:
    entries = [json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines() if line]
    status = {e["id"]: e["status"] for e in entries}
    wanted = [f"AUD-R{n:03d}" for n in range(82, 89)]
    assert {r: status.get(r) for r in wanted} == dict.fromkeys(wanted, "ACTIVE")
