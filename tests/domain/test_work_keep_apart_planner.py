"""The merge planner with Lance's "Keep apart" marks (artist model phase 3b PR 5c; SPEC D40,
D55, D56, S1; scope check R1; AUD-R089). Pure: facts and kept pairs in, sets and groups out.

A kept pair is a :class:`WorkPair`, its two work ids in order (the lower first, by plain string
order, as the database's ``COLLATE "C"`` check stores them). ``merge_sets`` takes the kept pairs
as an optional second argument; with none it plans exactly as before (the locked planner tests).

- **The pair's own joins drop (D40).** Two kept-apart works are never joined to each other:
  not by a work link, a shared artist or a shared name.
- **Every link edge (R1).** Works that share a work MBID are joined pair by pair, so dropping
  one kept pair leaves the others standing (two works of three that share an MBID still join
  when the third is kept apart from both).
- **Before Q15's count.** A kept pair's title join is dropped before the one-MBID rule counts
  the works a title key joins.
- **A held group (D56).** When other works still join two kept-apart works, the set comes out
  whole, its ``held`` naming the kept pairs inside it, and so does its group. Lance resolves it
  on the page; Apply never sends it.
- Pairs naming works the plan doesn't hold change nothing.
"""

from __future__ import annotations

import pytest

from backend.domain.work_merge_planner import (
    MergeJoin,
    MergeSet,
    PlannedGroup,
    WorkFacts,
    WorkPair,
    WorkPairError,
    merge_sets,
    plan_groups,
)

M1 = "0e000000-0000-4000-8000-00000000cc01"
M2 = "0e000000-0000-4000-8000-00000000cc02"


def facts(
    work_id: str,
    title: str,
    *,
    mbid: str | None = None,
    links: tuple[str, ...] = (),
    artists: tuple[str, ...] = (),
    names: tuple[str, ...] = (),
) -> WorkFacts:
    return WorkFacts(
        work_id=work_id,
        title_key=title,
        work_mbid=mbid,
        linked_mbids=frozenset(links),
        artists=frozenset(artists),
        names=frozenset(names),
    )


def pair(first: str, second: str) -> WorkPair:
    return WorkPair.of(first, second)


def plan(
    works: list[WorkFacts], kept: tuple[WorkPair, ...] = (), plays: dict[str, int] | None = None
) -> list[PlannedGroup]:
    return plan_groups(merge_sets(works, kept), plays or {})


# --- the pair ---------------------------------------------------------------------------------


def test_a_pair_is_its_two_works_in_order() -> None:
    assert WorkPair.of("w-b", "w-a") == WorkPair("w-a", "w-b") == WorkPair.of("w-a", "w-b")
    assert WorkPair.of("w-b", "w-a").first_work_id == "w-a"
    assert WorkPair.of("w-b", "w-a").second_work_id == "w-b"


def test_the_order_is_plain_string_order() -> None:
    """A hyphen sorts before a capital letter here, as in ``COLLATE "C"`` (a word-sort
    collation would ignore it and put ``workA`` first)."""
    assert WorkPair.of("workA", "work-b") == WorkPair("work-b", "workA")


@pytest.mark.parametrize(
    "first, second", [("w-b", "w-a"), ("w-a", "w-a")], ids=["out-of-order", "one-work"]
)
def test_a_pair_out_of_order_or_of_one_work_is_refused(first: str, second: str) -> None:
    with pytest.raises(WorkPairError):
        WorkPair(first, second)


def test_a_pair_of_one_work_is_refused_however_it_is_made() -> None:
    with pytest.raises(WorkPairError):
        WorkPair.of("w-a", "w-a")


# --- the pair's own joins drop ----------------------------------------------------------------


@pytest.mark.parametrize(
    "first, second",
    [
        (
            facts("w-a", "always", artists=("bon jovi",)),
            facts("w-b", "always", artists=("bon jovi",)),
        ),
        (facts("w-a", "always", names=("bon jovi",)), facts("w-b", "always", names=("bon jovi",))),
        (facts("w-a", "lady marmalade", mbid=M1), facts("w-b", "voulez vous", links=(M1,))),
    ],
    ids=["shared-artist", "shared-name", "work-link"],
)
def test_two_kept_apart_works_never_group(first: WorkFacts, second: WorkFacts) -> None:
    """D40: whatever joins them, a kept pair plans as two works alone; without the mark they
    group."""
    assert len(plan([first, second])) == 1

    assert plan([first, second], (pair("w-a", "w-b"),)) == []
    assert merge_sets([first, second], (pair("w-b", "w-a"),)) == []


def test_no_pairs_plan_as_before() -> None:
    works = [
        facts("w-labelle", "lady marmalade", mbid=M1, artists=("labelle",)),
        facts("w-aguilera", "lady marmalade", links=(M1,), artists=("aguilera",)),
        facts("w-band", "schools out", names=("alice cooper",)),
        facts("w-singer", "schools out", names=("alice cooper",)),
        facts("w-poison-1", "poison", artists=("alice cooper",)),
        facts("w-poison-2", "poison", artists=("bell biv devoe",)),
    ]

    assert merge_sets(works, ()) == merge_sets(works)
    assert [s.held for s in merge_sets(works)] == [frozenset(), frozenset()]


def test_a_pair_the_plan_does_not_hold_changes_nothing() -> None:
    works = [
        facts("w-a", "always", artists=("bon jovi",)),
        facts("w-b", "always", artists=("bon jovi",)),
    ]

    assert merge_sets(works, (pair("w-a", "w-gone"), pair("w-x", "w-y"))) == merge_sets(works)


# --- every link edge (R1) ---------------------------------------------------------------------


def test_the_other_works_of_one_work_mbid_still_join() -> None:
    """Three works share one work MBID. Lance keeps the first apart from both others; the other
    two still join on the link (a star from the first would have stranded them)."""
    works = [
        facts("w-a", "lady marmalade", mbid=M1, artists=("labelle",)),
        facts("w-b", "lady marmalade", links=(M1,), artists=("aguilera",)),
        facts("w-c", "voulez vous", links=(M1,), artists=("pink",)),
    ]

    assert plan(works, (pair("w-a", "w-b"), pair("w-a", "w-c"))) == [
        PlannedGroup(
            target_id="w-b",
            source_ids=("w-c",),
            joins=frozenset({MergeJoin.WORK_LINK}),
            plays=0,
        )
    ]


# --- before Q15's count -----------------------------------------------------------------------


def test_a_kept_title_join_drops_before_the_one_mbid_count() -> None:
    """One title key: a MusicBrainz work (M1) and a local work share an artist; the local work
    and another MusicBrainz work (M2) share a name. Joined, the three hold two work MBIDs, so no
    title join stands (Q15). With the local work kept apart from M2's work, the title joins
    left hold one MBID, and the first two group."""
    works = [
        facts("w-m1", "always", mbid=M1, artists=("bon jovi",)),
        facts("w-local", "always", artists=("bon jovi",), names=("bj",)),
        facts("w-m2", "always", mbid=M2, names=("bj",)),
    ]

    assert plan(works) == []
    assert plan(works, (pair("w-local", "w-m2"),)) == [
        PlannedGroup(
            target_id="w-m1",
            source_ids=("w-local",),
            joins=frozenset({MergeJoin.SHARED_ARTIST}),
            plays=0,
        )
    ]


# --- a held group (D56) -----------------------------------------------------------------------


def test_a_third_work_still_joining_a_kept_pair_holds_its_set() -> None:
    """Always and Always (live) are kept apart; Always (demo) shares the act with both, so the
    three still join, and the set says which pair it holds."""
    works = [
        facts("w-studio", "always", artists=("bon jovi",)),
        facts("w-live", "always", artists=("bon jovi",)),
        facts("w-demo", "always", artists=("bon jovi",)),
    ]
    kept = pair("w-studio", "w-live")

    assert merge_sets(works, (kept,)) == [
        MergeSet(
            work_ids=frozenset({"w-studio", "w-live", "w-demo"}),
            musicbrainz_ids=frozenset(),
            joins=frozenset({MergeJoin.SHARED_ARTIST}),
            held=frozenset({kept}),
        )
    ]


def test_a_held_group_keeps_its_place_and_names_its_pairs() -> None:
    """A held group is planned like any other (its target, sources and plays as usual, most
    played first), carrying its kept pairs; an unheld group carries none."""
    works = [
        facts("w-studio", "always", artists=("bon jovi",)),
        facts("w-live", "always", artists=("bon jovi",)),
        facts("w-demo", "always", artists=("bon jovi",)),
        facts("w-run-1", "runaway", artists=("bon jovi",)),
        facts("w-run-2", "runaway", artists=("bon jovi",)),
    ]
    kept = pair("w-studio", "w-live")
    plays = {"w-studio": 5, "w-live": 1, "w-run-1": 9}

    assert plan(works, (kept,), plays) == [
        PlannedGroup(
            target_id="w-run-1",
            source_ids=("w-run-2",),
            joins=frozenset({MergeJoin.SHARED_ARTIST}),
            plays=9,
        ),
        PlannedGroup(
            target_id="w-studio",
            source_ids=("w-live", "w-demo"),
            joins=frozenset({MergeJoin.SHARED_ARTIST}),
            plays=6,
            held=frozenset({kept}),
        ),
    ]


def test_every_pair_of_a_group_kept_apart_plans_nothing() -> None:
    """D55: Keep apart on a group marks every pair of its works, and then none of them join."""
    works = [
        facts("w-studio", "always", artists=("bon jovi",)),
        facts("w-live", "always", artists=("bon jovi",)),
        facts("w-demo", "always", artists=("bon jovi",)),
    ]
    every = (
        pair("w-studio", "w-live"),
        pair("w-studio", "w-demo"),
        pair("w-live", "w-demo"),
    )

    assert plan(works, every) == []


def test_undoing_one_pair_lets_those_two_join() -> None:
    """D55: with every pair kept but one, those two group and the third stays out."""
    works = [
        facts("w-studio", "always", artists=("bon jovi",)),
        facts("w-live", "always", artists=("bon jovi",)),
        facts("w-demo", "always", artists=("bon jovi",)),
    ]

    assert plan(works, (pair("w-studio", "w-live"), pair("w-live", "w-demo"))) == [
        PlannedGroup(
            target_id="w-demo",
            source_ids=("w-studio",),
            joins=frozenset({MergeJoin.SHARED_ARTIST}),
            plays=0,
        )
    ]
