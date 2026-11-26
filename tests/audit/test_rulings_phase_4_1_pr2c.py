"""The ruling phase 4.1 PR 2c adds (MusicBrainz cache fill: the pause at a run's end; SPEC S9;
D18).

AUD-R094 settles D18: a refusal that no request follows ends the batch there, so the refused
call is the run's last call: no pause and no further call, as S9 already said. PR 3's gate run
(D14) found the batch calling on instead, twice, at the end of a 2-hour run. When the run's 120
minutes end inside the pause, the run hands on no batch (the coordinator's reading of "the run's
last call"). It refs AUD-R093 (the request budget, whose pause it corrects) and supersedes
nothing: AUD-R093's ruling already says nothing waits when no request follows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LEDGER = Path(__file__).resolve().parents[2] / "audit" / "rulings.jsonl"

CHECK = "Check: tests/services/test_cache_fill_pause_at_run_end.py (4.1)."


def _ruling(ruling_id: str) -> dict[str, Any]:
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    [entry] = [e for e in map(json.loads, lines) if e["id"] == ruling_id]
    return dict(entry)


def test_r094_ends_the_batch_when_no_request_follows_a_refusal() -> None:
    ruling = _ruling("AUD-R094")

    assert ruling["status"] == "ACTIVE"
    assert "AUD-R093" in ruling["refs"]
    assert ruling["supersedes"] == []
    assert ruling["ruling"].endswith(CHECK)
    for words in (
        "D18",
        "no request follows",
        "ends the batch",
        "the run's last call",
        "10 s",
        "110 s",
        "120 minutes",
        "hands on no batch",
    ):
        assert words in ruling["ruling"], words
    for words in ("D14", "22:08:01", "22:08:03", "Fix it"):
        assert words in ruling["rationale"], words


def test_r093_still_stands() -> None:
    ruling = _ruling("AUD-R093")

    assert ruling["status"] == "ACTIVE"
    assert "nothing waits when no request follows" in ruling["ruling"]
