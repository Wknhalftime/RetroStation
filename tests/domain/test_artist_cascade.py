"""The cascade of a whole-name Reject or Unmatch, decided by one pure function (artist model
phase 1b, SPEC S1, S5, E1; M24, AUD-R037).

``plan_cascade(action, lines)`` takes the action and the log name's lines, each with its
status, whether its match is to a same-name file and its plays, and returns the lines kept and
the lines that change. Both actions keep the same lines:

- a manual line: ``manual_matched`` or ``manual_rejected`` (the curator decided it; today's
  Reject already leaves both alone);
- a line matched to a same-name file: ``auto_matched`` and its match is to a file whose artist
  is the name's (AUD-R022 D9, C16). A line in review whose candidate is a same-name file is not
  matched, so it changes.

Every other line changes. A line is on air while it is ``auto_matched`` or ``manual_matched``
(``play_file_resolution``, migration 0030), so the lines that go off air are the changed lines
that are on air. The counts the preview shows (S5) come from the plan: the changed lines on
air and their plays, and the kept lines that stay on air, by why they are kept (manual first).
"""

from __future__ import annotations

from itertools import product
from uuid import UUID, uuid4

import pytest

from backend.domain.artist_cascade import CascadeCounts, CascadeLine, plan_cascade
from backend.domain.enums import CascadeAction, MatchStatus

ON_AIR = {MatchStatus.AUTO_MATCHED, MatchStatus.MANUAL_MATCHED}
MANUAL = {MatchStatus.MANUAL_MATCHED, MatchStatus.MANUAL_REJECTED}


def _line(status: MatchStatus, *, same_name: bool = False, plays: int = 0) -> CascadeLine:
    return CascadeLine(identity_id=uuid4(), status=status, same_name=same_name, plays=plays)


def _ids(lines: tuple[CascadeLine, ...]) -> set[UUID]:
    return {line.identity_id for line in lines}


@pytest.mark.parametrize("action", list(CascadeAction))
def test_keeps_manual_and_same_name(action: CascadeAction) -> None:
    """S1: every status, with and without a same-name match, under both actions."""
    lines = [
        _line(status, same_name=same_name)
        for status, same_name in product(MatchStatus, (False, True))
    ]

    plan = plan_cascade(action, lines)

    expected_kept = {
        line.identity_id
        for line in lines
        if line.status in MANUAL or (line.status == MatchStatus.AUTO_MATCHED and line.same_name)
    }
    assert plan.action == action
    assert _ids(plan.kept) == expected_kept
    assert _ids(plan.changed) == {line.identity_id for line in lines} - expected_kept
    # Every line is in exactly one of the two, once.
    assert len(plan.kept) + len(plan.changed) == len(lines)


@pytest.mark.parametrize("action", list(CascadeAction))
def test_an_auto_matched_line_to_another_artists_file_changes(action: CascadeAction) -> None:
    """S1: an automatic match to a file of another artist (through the link) is not kept."""
    through_the_link = _line(MatchStatus.AUTO_MATCHED, same_name=False)

    plan = plan_cascade(action, [through_the_link])

    assert plan.kept == ()
    assert plan.changed == (through_the_link,)


@pytest.mark.parametrize("action", list(CascadeAction))
def test_a_review_line_with_a_same_name_candidate_changes(action: CascadeAction) -> None:
    """S1: only a matched line is "matched to a same-name file"; a line in review whose
    candidate is a same-name file is not on air and changes as today."""
    in_review = _line(MatchStatus.NEEDS_REVIEW, same_name=True)
    pending = _line(MatchStatus.PENDING, same_name=True)

    plan = plan_cascade(action, [in_review, pending])

    assert plan.kept == ()
    assert _ids(plan.changed) == {in_review.identity_id, pending.identity_id}


def test_the_plan_keeps_the_lines_order() -> None:
    lines = [
        _line(MatchStatus.AUTO_MATCHED),
        _line(MatchStatus.MANUAL_MATCHED),
        _line(MatchStatus.PENDING),
        _line(MatchStatus.AUTO_MATCHED, same_name=True),
    ]

    plan = plan_cascade(CascadeAction.REJECT, lines)

    assert plan.changed == (lines[0], lines[2])
    assert plan.kept == (lines[1], lines[3])


@pytest.mark.parametrize("action", list(CascadeAction))
def test_counts(action: CascadeAction) -> None:
    """S5: what goes off air (changed lines on air, and their plays) and what stays on air
    (kept lines on air, manual first, then same-name)."""
    lines = [
        _line(MatchStatus.AUTO_MATCHED, plays=40),  # off air
        _line(MatchStatus.AUTO_MATCHED, plays=2),  # off air
        _line(MatchStatus.AUTO_MATCHED, same_name=True, plays=900),  # stays: same name
        _line(MatchStatus.MANUAL_MATCHED, plays=7),  # stays: manual
        _line(MatchStatus.MANUAL_MATCHED, same_name=True, plays=3),  # stays: manual
        _line(MatchStatus.MANUAL_REJECTED, plays=5),  # kept, but not on air
        _line(MatchStatus.NEEDS_REVIEW, plays=11),  # changes, but was not on air
        _line(MatchStatus.PENDING, same_name=True, plays=13),  # changes, but was not on air
        _line(MatchStatus.AUTO_REJECTED, plays=17),  # changes, but was not on air
    ]

    counts = plan_cascade(action, lines).counts()

    assert counts == CascadeCounts(
        off_air_lines=2, off_air_plays=42, kept_manual=2, kept_same_name=1
    )


def test_off_air_lines_are_the_changed_lines_on_air() -> None:
    lines = [
        _line(status, same_name=same_name, plays=1)
        for status, same_name in product(MatchStatus, (False, True))
    ]

    plan = plan_cascade(CascadeAction.UNMATCH, lines)

    assert _ids(plan.off_air()) == {
        line.identity_id for line in plan.changed if line.status in ON_AIR
    }
    assert {line.status for line in plan.off_air()} == {MatchStatus.AUTO_MATCHED}


@pytest.mark.parametrize("action", list(CascadeAction))
def test_empty_name(action: CascadeAction) -> None:
    """E1: a name with no lines, or none matched, goes 0 / 0 and keeps 0."""
    nothing = plan_cascade(action, [])
    unmatched = plan_cascade(
        action, [_line(MatchStatus.PENDING, plays=4), _line(MatchStatus.AUTO_REJECTED, plays=9)]
    )

    assert (nothing.kept, nothing.changed) == ((), ())
    assert nothing.counts() == CascadeCounts(0, 0, 0, 0)
    assert unmatched.counts() == CascadeCounts(0, 0, 0, 0)


def test_the_plan_is_pure() -> None:
    """S1: no I/O and no mutation: the same input gives the same plan, and the lines handed in
    come back unchanged."""
    lines = [_line(MatchStatus.AUTO_MATCHED, plays=3), _line(MatchStatus.MANUAL_MATCHED)]
    snapshot = list(lines)

    first = plan_cascade(CascadeAction.REJECT, lines)
    second = plan_cascade(CascadeAction.REJECT, iter(lines))

    assert first == second
    assert lines == snapshot
    assert first.changed[0] == lines[0]
