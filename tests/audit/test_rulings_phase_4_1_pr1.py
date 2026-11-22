"""The ruling phase 4.1 PR 1 adds (MusicBrainz cache fill; SPEC S1, S2, S6, E2, E3, E5; D2, D3).

AUD-R090 settles PR 1's part of AUD-R042: the logged cache, the client's three new cached
methods, the artist stage's outage deferral (AUD-R028 extended, D2) and the 503 on the one
MusicBrainz route. It amends nothing AUD-R042 says, so it supersedes nothing, and its check files
all arrive in PR 1, so nothing is pending. AUD-R042's own 4.1 marker stays until PR 2, whose
``test_cache_fill.py`` it names (``test_rulings_phase_2a.py`` pins it, relocked then).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LEDGER = Path(__file__).resolve().parents[2] / "audit" / "rulings.jsonl"

CHECK = (
    "Check: tests/integration/test_mb_cache_logged_pg.py (4.1), "
    "tests/services/test_mb_client_fill_methods.py (4.1), "
    "tests/services/test_artist_stage_outage_defer.py (4.1), "
    "tests/routers/test_mb_routes_503.py (4.1)."
)


def _ruling(ruling_id: str) -> dict[str, Any]:
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    [entry] = [e for e in map(json.loads, lines) if e["id"] == ruling_id]
    return dict(entry)


def test_r090_settles_pr1_of_r042() -> None:
    ruling = _ruling("AUD-R090")

    assert ruling["status"] == "ACTIVE"
    assert {"AUD-R042", "AUD-R028", "AUD-R102"} <= set(ruling["refs"])
    assert ruling["supersedes"] == []
    assert ruling["ruling"].endswith(CHECK)
    for words in (
        "LOGGED",
        "recording-name-title:",
        "artist-aliases:",
        "release-missing:",
        "DEFERRED_RETRY",
        "ARTIST_MB_FAILURE_CUTOFF",
        "503",
    ):
        assert words in ruling["ruling"], words
    assert "D2" in ruling["rationale"] and "D3" in ruling["rationale"]
