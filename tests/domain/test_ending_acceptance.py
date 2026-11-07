"""Acceptance: the stage 1 ending rules place EOM closer to Lance than autocue does.

Spec: docs/superpowers/specs/2026-11-07-eom-ending-rules-design.md ("Acceptance"); plan:
docs/superpowers/plans/2026-11-07-eom-ending-rules-stage1.md (Task 1). The fixture holds Lance's
51 labelled picks (2026-11-07), each with the song's stored autocue cues and loudness tail
(``scripts/extract_ending_fixtures.py``).

The split is fixed before any tuning: a pick is held back when the sha256 of its audio hash is
odd. Rules are tuned on the other half and judged here on the held-back half, so they cannot
simply memorize Lance's answers. A pick is judged under Lance's tag, never the classifier's.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

import pytest

from backend.domain.endings import Follows, LoudnessTail, classify_ending, ruled_cues
from backend.domain.streaming import CuePoints

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "endings" / "lance_picks.json"

SUSTAINED_BAR_MS = 1500
"""Lance's ruling at the test lock: the design's 1000, or the plan's proposed 1500."""

SUSTAINED_GAIN_MS = 500
"""The rule must beat autocue on sustained endings by at least this much (plan, Task 1)."""

COLD_SLACK_MS = 50
COLD_BAR_MS = 300
WITHIN_MS = 1000
FALSE_ENDING_SLACK_MS = 500
HARD_STOP_DB = 15.0
HARD_STOP_WINDOW_MS = 1000


def _picks() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return loaded


def _held_back(pick: dict[str, Any]) -> bool:
    digest = hashlib.sha256(pick["audio_hash"].encode()).hexdigest()
    return int(digest, 16) % 2 == 1


def _cues(pick: dict[str, Any]) -> CuePoints:
    return CuePoints(**pick["cues"])


def _tail(pick: dict[str, Any]) -> LoudnessTail:
    tail = pick["tail"]
    return LoudnessTail(tail["start_ms"], tail["frame_ms"], tuple(tail["level_db"]))


def _autocue_eom(pick: dict[str, Any]) -> int:
    cues = _cues(pick)
    return cues.cue_out_ms - cues.start_next_ms


def _rule_eom(pick: dict[str, Any]) -> int:
    cues = _cues(pick)
    played = ruled_cues(cues, classify_ending(_tail(pick), cues), Follows.SONG)
    return played.cue_out_ms - played.start_next_ms


def _error(eom_ms: int, pick: dict[str, Any]) -> int:
    return abs(eom_ms - pick["lance_eom_ms"])


def _held(tag: str | None = None) -> list[dict[str, Any]]:
    return [p for p in _picks() if _held_back(p) and (tag is None or p["tag"] == tag)]


def _medians(picks: list[dict[str, Any]]) -> tuple[float, float]:
    """(rule, autocue) median error over ``picks``."""
    assert picks, "the held-back half has no picks with this tag"
    rule = statistics.median(_error(_rule_eom(p), p) for p in picks)
    autocue = statistics.median(_error(_autocue_eom(p), p) for p in picks)
    return rule, autocue


def _smoothed_db(levels: tuple[float, ...], frame_ms: int, at: int) -> float:
    """The 400 ms trailing mean energy ending at frame ``at``, in dB (the hard-stop test)."""
    width = max(1, 400 // frame_ms)
    window = levels[max(0, at - width + 1) : at + 1]
    return 10 * math.log10(sum(10 ** (v / 10) for v in window) / len(window))


SILENCE_DB = -120.0
"""Frames past the end of the file count as silence (a song that stops as the file ends)."""


def _hard_stop(pick: dict[str, Any]) -> bool:
    """The level drops 15 dB or more within 1 s of autocue's EOM."""
    tail = _tail(pick)
    at = (_autocue_eom(pick) - tail.start_ms) // tail.frame_ms
    later = at + HARD_STOP_WINDOW_MS // tail.frame_ms
    if at < 0 or at >= len(tail.level_db):
        return False
    levels = tail.level_db + (SILENCE_DB,) * max(0, later + 1 - len(tail.level_db))
    before = _smoothed_db(levels, tail.frame_ms, at)
    after = _smoothed_db(levels, tail.frame_ms, later)
    return before - after >= HARD_STOP_DB


def test_fixture_holds_all_51_picks() -> None:
    assert len(_picks()) == 51


def test_cold_no_worse_than_autocue() -> None:
    rule, autocue = _medians(_held("cold"))
    assert rule <= autocue + COLD_SLACK_MS
    assert rule <= COLD_BAR_MS


@pytest.mark.parametrize(
    "pick", [p for p in _picks() if p["tag"] == "cold"], ids=lambda p: p["title"]
)
def test_cold_hard_stops_keep_autocue_eom(pick: dict[str, Any]) -> None:
    """Anti-goal: a cold ending with a hard stop keeps autocue's EOM and stops on its own:
    no fade, autocue's Stop."""
    if not _hard_stop(pick):
        pytest.skip("no hard stop at autocue's EOM")
    cues = _cues(pick)
    played = ruled_cues(cues, classify_ending(_tail(pick), cues), Follows.SONG)
    assert _rule_eom(pick) == _autocue_eom(pick)
    assert (played.cue_out_ms, played.fade_out_ms) == (cues.cue_out_ms, 0)


def test_most_cold_picks_have_a_hard_stop() -> None:
    """Guards the check above against skipping everything: 12 of the 15 cold picks stop hard
    (2026-11-07 fixture)."""
    assert sum(_hard_stop(p) for p in _picks() if p["tag"] == "cold") >= 12


def test_fade_no_worse_than_autocue() -> None:
    rule, autocue = _medians(_held("fade"))
    assert rule <= autocue


def test_sustained_beats_autocue() -> None:
    rule, autocue = _medians(_held("sustained"))
    assert rule <= SUSTAINED_BAR_MS
    assert rule <= autocue - SUSTAINED_GAIN_MS


def test_more_picks_within_a_second() -> None:
    held = _held()
    rule = sum(_error(_rule_eom(p), p) <= WITHIN_MS for p in held)
    autocue = sum(_error(_autocue_eom(p), p) <= WITHIN_MS for p in held)
    assert rule >= autocue


def test_false_ending_not_worse() -> None:
    (pick,) = [p for p in _picks() if p["tag"] == "false_ending"]
    assert _error(_rule_eom(pick), pick) <= _error(_autocue_eom(pick), pick) + FALSE_ENDING_SLACK_MS
