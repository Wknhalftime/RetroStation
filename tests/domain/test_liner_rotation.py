"""Station liners, PR L2 Task 8: the rotation and cadence rules in ``backend/domain/liners.py``.

Requirements: LR7 (a liner is due once the session has handed out ``cadence`` songs since the
last one; a blank cadence never is), LR8 (a shuffle per session, played through before it
reshuffles; never the same liner twice in a row across a reshuffle unless there is only one;
a new liner joins at the reshuffle; a deleted one is skipped). Plan note 11: ``next_liner``
first drops ids no longer available; with nothing left in the order it reshuffles the
available ids through the injected ``shuffle``; when that would put ``last`` first and there
is more than one liner, it swaps the first and last entries.

The shuffle is a port (``Shuffle``, in place): these tests inject deterministic ones and
record what each was given.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from backend.domain.liners import (
    LinerCadence,
    LinerRotation,
    Shuffle,
    liner_due,
    next_liner,
)

A = UUID("aaaaaaaa-0000-4000-8000-00000000000a")
B = UUID("bbbbbbbb-0000-4000-8000-00000000000b")
C = UUID("cccccccc-0000-4000-8000-00000000000c")
D = UUID("dddddddd-0000-4000-8000-00000000000d")


@dataclass
class Recorded:
    """A shuffle that records a copy of each list it is given, then applies ``move``."""

    move: Shuffle
    given: list[list[UUID]] = field(default_factory=list)

    def __call__(self, ids: list[UUID]) -> None:
        self.given.append(list(ids))
        self.move(ids)


def reverse(ids: list[UUID]) -> None:
    ids.reverse()


def keep(ids: list[UUID]) -> None:
    """Leaves the order as given: here that puts the last-played liner first."""


def test_first_pick_shuffles_all_available() -> None:
    """LR8: the first pick shuffles every available liner into an order and plays its head;
    the rest of the order waits, and the pick is remembered as ``last``."""
    shuffle = Recorded(reverse)

    picked, rotation = next_liner(LinerRotation(), [A, B, C], shuffle)

    assert picked == C
    assert rotation == LinerRotation((B, A), C)
    assert shuffle.given == [[A, B, C]]


def test_plays_through_the_order_before_reshuffling() -> None:
    """LR8: the order plays through, one liner per pick, with no reshuffle until it is empty;
    then it reshuffles (here the reshuffle starts with C, which is not the last played)."""
    shuffle = Recorded(reverse)
    rotation = LinerRotation()
    picks: list[UUID | None] = []
    for _ in range(3):
        picked, rotation = next_liner(rotation, [A, B, C], shuffle)
        picks.append(picked)

    assert picks == [C, B, A]
    assert rotation == LinerRotation((), A)
    assert len(shuffle.given) == 1

    picked, rotation = next_liner(rotation, [A, B, C], shuffle)

    assert picked == C
    assert rotation == LinerRotation((B, A), C)
    assert len(shuffle.given) == 2


def test_reshuffle_never_repeats_the_last() -> None:
    """LR8 (spec "Which liner", plan note 11): a reshuffle that would put the liner that just
    played first has its first and last entries swapped."""
    shuffle = Recorded(keep)

    picked, rotation = next_liner(LinerRotation((), A), [A, B, C], shuffle)

    assert picked == C
    assert rotation == LinerRotation((B, A), C)
    assert shuffle.given == [[A, B, C]]


def test_single_liner_repeats() -> None:
    """LR8: with only one liner it plays at every slot."""
    rotation = LinerRotation()
    picks: list[UUID | None] = []
    for _ in range(3):
        picked, rotation = next_liner(rotation, [A], reverse)
        picks.append(picked)

    assert picks == [A, A, A]
    assert rotation == LinerRotation((), A)


def test_new_liner_joins_at_reshuffle() -> None:
    """LR8: a liner uploaded mid-session (D) is not in the order, so it waits for the order to
    empty and joins the reshuffle."""
    picked, rotation = next_liner(LinerRotation(), [A, B, C], reverse)
    assert picked == C
    picks: list[UUID | None] = []
    for _ in range(3):
        picked, rotation = next_liner(rotation, [A, B, C, D], reverse)
        picks.append(picked)

    assert picks == [B, A, D]
    assert rotation == LinerRotation((C, B, A), D)


def test_deleted_liner_is_skipped() -> None:
    """LR8, Review Focus 3: a liner deleted while the order holds it (B) is dropped when its
    turn comes; the next one plays instead."""
    picked, rotation = next_liner(LinerRotation((B, A), C), [A, C], reverse)

    assert picked == A
    assert rotation == LinerRotation((), A)


def test_nothing_available() -> None:
    """LR8, LR10: with no liner available there is no pick, the order empties, and the last
    played is kept."""
    assert next_liner(LinerRotation((B,), A), [], reverse) == (None, LinerRotation((), A))
    assert next_liner(LinerRotation(), [], reverse) == (None, LinerRotation((), None))


def test_liner_due() -> None:
    """LR7: due once the songs since the last liner reach the cadence; never without one."""
    assert liner_due(2, LinerCadence(3)) is False
    assert liner_due(3, LinerCadence(3)) is True
    assert liner_due(4, LinerCadence(3)) is True
    assert liner_due(9, None) is False
    assert liner_due(0, LinerCadence(1)) is False
    assert liner_due(1, LinerCadence(1)) is True
