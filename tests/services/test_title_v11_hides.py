"""Acceptance: V11.1 decides what is hidden (SPEC 2026-11-02, H1 and H5; AUD-R101).

Blind grade 1 (2026-11-01): of the 135 suggestions Lance rejected, V11.1 hid 123 and today's
scorer 4, and V11.1 hid none of the 33 songs he accepted. So a song V11.1 hides is hidden even
when today's scorer would show or auto-match it. Auto-matching still needs both scorers on the
same file (test_title_trial_mode.py), until a later check of V11.1's solo auto-match calls.

The world and its score pinning are test_title_trial_mode.py's: today's scores pinned per file,
V11.1's per file title, or both real scorers on real titles.
"""

from __future__ import annotations

import pytest

from backend.domain.enums import MatchStatus, ReasonCode
from tests.services.test_title_trial_mode import _resolved


@pytest.mark.parametrize(
    ("today", "v11"),
    [(70.0, 40.0), (97.0, 31.0)],
    ids=["today_shows", "today_auto_matches"],
)
def test_v11_hides_what_today_shows(
    monkeypatch: pytest.MonkeyPatch, today: float, v11: float
) -> None:
    """H1: hidden whether today's scorer would review (70) or auto-match (97, a lone file)."""
    world = _resolved(monkeypatch)
    world.file("A", today=today, v11=v11)
    song = world.song()

    world.run()

    stored = world.stored(song)
    assert stored.match_status == MatchStatus.AUTO_REJECTED
    assert stored.reason_code == ReasonCode.TITLE_SCORES_DISAGREE
    assert stored.reason_detail is not None
    assert str(round(today)) in stored.reason_detail
    assert str(round(v11)) in stored.reason_detail
    # Hidden songs write no match row (AUD-R022 D6).
    assert world.rows(song) == []


def test_v11_hides_what_today_auto_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """H1 with a real competitor: today auto-matches A (97, a 77-point lead); V11.1 hides all."""
    world = _resolved(monkeypatch)
    world.file("A", today=97.0, v11=31.0)
    world.file("B", today=20.0, v11=12.0)
    song = world.song()

    world.run()

    stored = world.stored(song)
    assert stored.match_status == MatchStatus.AUTO_REJECTED
    assert stored.reason_code == ReasonCode.TITLE_SCORES_DISAGREE
    assert world.rows(song) == []


def test_real_scorers_hide_hide_vs_ode(monkeypatch: pytest.MonkeyPatch) -> None:
    """H5: a pair from blind grade 1. Today shows "Hide" against "Ode" at 57; V11.1 says 29,
    and Lance rejected it."""
    world = _resolved(monkeypatch, pin_today=False)
    world.file("Ode")
    song = world.song("Hide")

    world.run(real_v11=True)

    stored = world.stored(song)
    assert stored.match_status == MatchStatus.AUTO_REJECTED
    assert stored.reason_code == ReasonCode.TITLE_SCORES_DISAGREE
    assert world.rows(song) == []
