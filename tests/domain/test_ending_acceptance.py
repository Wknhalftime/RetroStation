"""Acceptance: the stage 1 ending rules keep autocue's EOM and change only the fade after it.

Lance (2026-11-07, "Fades only, EOM as-is"): moving EOM to the measured end of the full-volume
part failed this file's held-back check (sustained 3000 vs autocue's 2673 ms, fades 570 vs
211 ms), so every ending keeps autocue's EOM. The fade after EOM follows his table: none after
a cold ending, 2 s after a long tail, 1 s after anything else (a song next).

With EOM always autocue's, comparing the rule's EOM with autocue's against Lance's picks can no
longer fail, so those checks are gone (final review, 2026-11-07). What is graded here is what
stage 1 plays: autocue's EOM everywhere, no fade after a cold ending, and the table's fade.

Spec: docs/superpowers/specs/2026-11-07-eom-ending-rules-design.md; plan:
docs/superpowers/plans/2026-11-07-eom-ending-rules-stage1.md. The fixture holds Lance's 51
labelled picks (2026-11-07), each with the song's stored autocue cues and loudness tail
(``scripts/extract_ending_fixtures.py``); stage 2 re-uses it.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from backend.domain.endings import (
    FADE_AFTER_EOM_MS,
    EndingType,
    Follows,
    LoudnessTail,
    classify_ending,
    ruled_cues,
)
from backend.domain.streaming import CuePoints

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "endings" / "lance_picks.json"

HARD_STOP_DB = 15.0
HARD_STOP_WINDOW_MS = 1000


def _picks() -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return loaded


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


@pytest.mark.parametrize("pick", _picks(), ids=lambda p: p["title"])
def test_every_eom_is_autocues(pick: dict[str, Any]) -> None:
    """Lance (2026-11-07): every pick plays autocue's EOM."""
    assert _rule_eom(pick) == _autocue_eom(pick)


@pytest.mark.parametrize("pick", _picks(), ids=lambda p: p["title"])
def test_fade_after_eom_follows_the_table(pick: dict[str, Any]) -> None:
    """Lance's fade after EOM, a song next: none after a cold ending (autocue's Stop);
    otherwise the table's fade from EOM, cut short only by autocue's Stop."""
    cues = _cues(pick)
    ending = classify_ending(_tail(pick), cues)
    played = ruled_cues(cues, ending, Follows.SONG)
    eom = _autocue_eom(pick)
    if ending.ending is EndingType.COLD:
        assert (played.cue_out_ms, played.fade_out_ms) == (cues.cue_out_ms, 0)
        return
    fade = min(FADE_AFTER_EOM_MS[(ending.ending, Follows.SONG)], cues.cue_out_ms - eom)
    assert (played.cue_out_ms, played.fade_out_ms, played.start_next_ms) == (
        eom + fade,
        fade,
        fade,
    )
