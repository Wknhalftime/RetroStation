"""The ruling phase 4.1 PR 3 adds (MusicBrainz cache fill: work links; SPEC S5; D1, D13, D14; R1,
R7).

AUD-R092 adds the fill's work-link tier, capped at 3,000 and placed before the tail (D13): one
release lookup through ``lookup_release_for_links`` for each performance whose recording has no
known work link, a ``recording-work:{rid}`` row for every track, and the merge list's planner
reading those rows beside ``recording:`` rows. Each lookup is a fill call under AUD-R093's budget.
It supersedes two rulings in part, quoting each one's own words: AUD-R080's work-MBID join (a
cached recording lookup's link, now also a fetched one, R1) and AUD-R091's tier order (D13). Both
stay ACTIVE.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

LEDGER = Path(__file__).resolve().parents[2] / "audit" / "rulings.jsonl"

CHECK = (
    "Check: tests/tasks/test_work_link_fetch.py (4.1), "
    "tests/integration/test_planner_reads_fetched_links_pg.py (4.1)."
)

# An "(in part)" entry: the ruling's id, then its own words in single quotes.
_IN_PART = re.compile(r"^(?P<id>AUD-R\d{3}) \(in part\): '(?P<words>[^']+)': ")


def _ruling(ruling_id: str) -> dict[str, Any]:
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    [entry] = [e for e in map(json.loads, lines) if e["id"] == ruling_id]
    return dict(entry)


def test_r092_settles_the_work_links() -> None:
    ruling = _ruling("AUD-R092")

    assert ruling["status"] == "ACTIVE"
    assert {"AUD-R080", "AUD-R089", "AUD-R091", "AUD-R093", "AUD-R103"} <= set(ruling["refs"])
    assert ruling["ruling"].endswith(CHECK)
    for words in (
        "work_links",
        "3,000",
        "before the tail",
        "lookup_release_for_links",
        "release-missing:",
        "recording-work:{rid}",
        "_LINKED",
        "AUD-R093",
        "no re-check",
        "Apply",
        "sweep_empty_works_task",
    ):
        assert words in ruling["ruling"], words
    for words in ("D1", "D5", "D13", "D14", "R1", "30,918", "2,810", "1.6", "40%"):
        assert words in ruling["rationale"], words


def test_r092_quotes_what_it_supersedes() -> None:
    """Each "(in part)" entry quotes the superseded ruling's own words, which are still there."""
    entries = [_IN_PART.match(s) for s in _ruling("AUD-R092")["supersedes"]]

    assert all(entries)
    quoted = {m["id"]: m["words"] for m in entries if m is not None}
    assert set(quoted) == {"AUD-R080", "AUD-R091"}
    for ruling_id, words in quoted.items():
        assert words in _ruling(ruling_id)["ruling"], ruling_id
    assert "cached recording lookup" in quoted["AUD-R080"]
    assert "tiers, in order" in quoted["AUD-R091"]


def test_r080_and_r091_still_stand() -> None:
    assert _ruling("AUD-R080")["status"] == "ACTIVE"
    assert _ruling("AUD-R091")["status"] == "ACTIVE"
