"""The merge planner (artist model phase 3b PR 4; SPEC S1, D17 (scope Q14, Q15), D29; design
§6.2, M18, M29; AUD-R080). Pure: it reads facts, never the database.

Each work comes in as :class:`WorkFacts`: its title key (grouping's exact base-title rule, D29),
its work MBID if it is a MusicBrainz work, the work MBIDs its performances' cached recording
lookups name (M29), its credit artists (each credit as its artist's merge-group primary, else its
MBID; placeholders left out by the caller) and its credited names (normalized).

Two works join (§6.2) when:

- they share a work MBID, their own or one a cached lookup names (``work link``), whatever
  their titles: covers by other acts and one composition under different titles;
- or their title keys are equal and they share a credit artist (``shared artist``);
- or their title keys are equal and they share a credited name (``shared name``, M18).

A title alone never joins. Joins are transitive (Q14). A title join never brings together two
different work MBIDs (Q15): when the works one title key joins hold two or more, none of its
title joins stand (a work link still does).

Each set of joined works becomes one group. Its target is a MusicBrainz work, then the work with
more matched plays, then the lower id; the sources follow in the same order. Groups come most
played first.
"""

from __future__ import annotations

import pytest

from backend.domain.work_merge_planner import (
    MergeJoin,
    MergeSet,
    PlannedGroup,
    WorkFacts,
    merge_sets,
    plan_groups,
)
from backend.services.merge_plan_service import title_key


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


def plan(works: list[WorkFacts], plays: dict[str, int] | None = None) -> list[PlannedGroup]:
    return plan_groups(merge_sets(works), plays or {})


def group(
    target: str, sources: tuple[str, ...], joins: set[MergeJoin], plays: int = 0
) -> PlannedGroup:
    return PlannedGroup(target_id=target, source_ids=sources, joins=frozenset(joins), plays=plays)


MARMALADE = "0e000000-0000-4000-8000-00000000aa01"
VOULEZ = "0e000000-0000-4000-8000-00000000aa02"


# --- the joins ------------------------------------------------------------------------------------


def test_lady_marmalade_joins_on_the_work_link() -> None:
    """LaBelle's "Lady Marmalade" is the MusicBrainz work; Christina Aguilera's cover is a local
    work whose recording lookup names it, and so does a local work under another title. No
    artist or name is shared: the link alone joins them."""
    works = [
        facts("w-labelle", "lady marmalade", mbid=MARMALADE, artists=("labelle",)),
        facts("w-aguilera", "lady marmalade", links=(MARMALADE,), artists=("aguilera",)),
        facts("w-voulez", "voulez vous coucher avec moi", links=(MARMALADE,), artists=("x",)),
    ]

    assert plan(works) == [
        group("w-labelle", ("w-aguilera", "w-voulez"), {MergeJoin.WORK_LINK}),
    ]


def test_two_local_works_naming_one_work_join() -> None:
    """Neither is the MusicBrainz work; both lookups name it."""
    works = [
        facts("w-a", "lady marmalade", links=(MARMALADE,), artists=("labelle",)),
        facts("w-b", "lady marmalade", links=(MARMALADE,), artists=("aguilera",)),
    ]

    assert plan(works, {"w-a": 3, "w-b": 9}) == [
        group("w-b", ("w-a",), {MergeJoin.WORK_LINK}, plays=12),
    ]


def test_alice_cooper_shares_a_credited_name() -> None:
    """The band Alice Cooper and the singer Alice Cooper are two artists (two MBIDs) of one
    name. Their "School's Out" works share no artist but share the credited name (M18)."""
    works = [
        facts("w-band", "schools out", artists=("mbid-band",), names=("alice cooper",)),
        facts("w-singer", "schools out", artists=("mbid-singer",), names=("alice cooper",)),
    ]

    assert plan(works) == [group("w-band", ("w-singer",), {MergeJoin.SHARED_NAME})]


def test_poison_by_title_alone_never_merges() -> None:
    """Alice Cooper's "Poison" and Bell Biv DeVoe's "Poison": the same title, no artist or name
    shared (2,048 shared titles on dev)."""
    works = [
        facts("w-cooper", "poison", artists=("cooper",), names=("alice cooper",)),
        facts("w-bbd", "poison", artists=("bbd",), names=("bell biv devoe",)),
    ]

    assert plan(works) == []


def test_janet_and_janet_jackson_join_by_merge_group_primary() -> None:
    """2a merged "Janet" into "Janet Jackson": both works' credits come in as the group's primary,
    so they share an artist though their credited names differ."""
    works = [
        facts("w-janet", "together again", artists=("janet-jackson",), names=("janet",)),
        facts("w-jackson", "together again", artists=("janet-jackson",), names=("janet jackson",)),
    ]

    assert plan(works) == [group("w-jackson", ("w-janet",), {MergeJoin.SHARED_ARTIST})]


def test_a_shared_artist_needs_the_same_title_key() -> None:
    works = [
        facts("w-always", "always", artists=("bon-jovi",)),
        facts("w-bed", "bed of roses", artists=("bon-jovi",)),
    ]

    assert plan(works) == []


def test_an_empty_title_key_never_joins() -> None:
    """A title that normalizes to nothing gives no key: two such works of one act stay apart."""
    works = [facts("w-1", "", artists=("act",)), facts("w-2", "", artists=("act",))]

    assert plan(works) == []


def test_pairs_join_transitively() -> None:
    """A and B share an artist; B and C a work link; C and D a name. One group (Q14)."""
    works = [
        facts("w-a", "always", artists=("bon-jovi",)),
        facts("w-b", "always", artists=("bon-jovi",), links=(VOULEZ,)),
        facts("w-c", "always (other)", links=(VOULEZ,), names=("x",)),
        facts("w-d", "always (other)", names=("x",)),
    ]

    [planned] = plan(works)

    assert {planned.target_id, *planned.source_ids} == {"w-a", "w-b", "w-c", "w-d"}
    assert planned.joins == {
        MergeJoin.SHARED_ARTIST,
        MergeJoin.WORK_LINK,
        MergeJoin.SHARED_NAME,
    }


# --- two work MBIDs (Q15) -------------------------------------------------------------------------


def test_two_work_mbids_never_merge_by_title() -> None:
    """Two MusicBrainz works of one act under one title are two compositions. A local work of
    that act and title joins neither: which one it is can't be told (Q15)."""
    works = [
        facts("w-x", "intro", mbid="mbid-x", artists=("act",)),
        facts("w-y", "intro", mbid="mbid-y", artists=("act",)),
        facts("w-local", "intro", artists=("act",)),
    ]

    assert plan(works) == []


def test_a_title_join_stands_beside_one_work_mbid() -> None:
    works = [
        facts("w-x", "intro", mbid="mbid-x", artists=("act",)),
        facts("w-local", "intro", artists=("act",)),
    ]

    assert plan(works, {"w-local": 50}) == [
        group("w-x", ("w-local",), {MergeJoin.SHARED_ARTIST}, plays=50),
    ]


def test_the_rule_is_per_set_of_works_a_title_joins() -> None:
    """Two acts' "Intro"s, each a MusicBrainz work with a local copy of the same act: the title
    joins nothing across the acts, so each pair holds one work MBID and merges."""
    works = [
        facts("w-a-x", "intro", mbid="mbid-x", artists=("act-a",)),
        facts("w-a-local", "intro", artists=("act-a",)),
        facts("w-b-y", "intro", mbid="mbid-y", artists=("act-b",)),
        facts("w-b-local", "intro", artists=("act-b",)),
    ]

    assert plan(works, {"w-a-local": 2, "w-b-local": 1}) == [
        group("w-a-x", ("w-a-local",), {MergeJoin.SHARED_ARTIST}, plays=2),
        group("w-b-y", ("w-b-local",), {MergeJoin.SHARED_ARTIST}, plays=1),
    ]


def test_a_title_join_is_named_by_its_stronger_reason() -> None:
    """A shared artist over a shared name; a work link between the same works is listed
    beside it."""
    works = [
        facts("w-1", "always", links=("mbid-a",), artists=("a",), names=("bon jovi",)),
        facts("w-2", "always", links=("mbid-a",), artists=("a",), names=("bon jovi",)),
        facts("w-3", "runaway", artists=("a",), names=("bon jovi",)),
        facts("w-4", "runaway", artists=("a",), names=("bon jovi",)),
    ]

    assert [g.joins for g in plan(works)] == [
        {MergeJoin.WORK_LINK, MergeJoin.SHARED_ARTIST},
        {MergeJoin.SHARED_ARTIST},
    ]


def test_a_link_joins_even_where_two_work_mbids_meet_by_title() -> None:
    """The title joins of "intro" go (two MBIDs), but w-cover's lookup names mbid-x: that link
    stands."""
    works = [
        facts("w-x", "intro", mbid="mbid-x", artists=("act",)),
        facts("w-y", "intro", mbid="mbid-y", artists=("act",)),
        facts("w-cover", "intro", links=("mbid-x",), artists=("act",)),
    ]

    assert plan(works) == [group("w-x", ("w-cover",), {MergeJoin.WORK_LINK})]


# --- the target and the order ---------------------------------------------------------------------


def test_the_target_is_a_musicbrainz_work_then_more_plays() -> None:
    """Q14: a MusicBrainz work first, though another has more plays; else more plays; then the
    lower id. The sources follow in the same order."""
    musicbrainz = [
        facts("w-1", "always", artists=("a",)),
        facts("w-2", "always", mbid="mbid-always", artists=("a",)),
        facts("w-3", "always", artists=("a",)),
    ]
    by_plays = [
        facts("w-4", "bed of roses", artists=("a",)),
        facts("w-5", "bed of roses", artists=("a",)),
        facts("w-6", "bed of roses", artists=("a",)),
    ]
    tied = [facts("w-8", "runaway", artists=("a",)), facts("w-7", "runaway", artists=("a",))]
    plays = {"w-1": 90, "w-2": 1, "w-3": 5, "w-4": 2, "w-5": 40, "w-6": 7}

    assert plan(musicbrainz + by_plays + tied, plays) == [
        group("w-2", ("w-1", "w-3"), {MergeJoin.SHARED_ARTIST}, plays=96),
        group("w-5", ("w-6", "w-4"), {MergeJoin.SHARED_ARTIST}, plays=49),
        group("w-7", ("w-8",), {MergeJoin.SHARED_ARTIST}, plays=0),
    ]


def test_merge_sets_carry_their_musicbrainz_works() -> None:
    works = [
        facts("w-1", "always", artists=("a",)),
        facts("w-2", "always", mbid="mbid-always", artists=("a",)),
        facts("w-lone", "lonely", artists=("a",)),
    ]

    assert merge_sets(works) == [
        MergeSet(
            work_ids=frozenset({"w-1", "w-2"}),
            musicbrainz_ids=frozenset({"w-2"}),
            joins=frozenset({MergeJoin.SHARED_ARTIST}),
        )
    ]


def test_no_groups_is_an_empty_plan() -> None:
    assert plan([]) == []
    assert plan([facts("w-1", "always", artists=("a",))]) == []


# --- D29: grouping's exact title rule -------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Sweet Child O' Mine", "Sweet Child O' Mine (Remastered 2018)"),
        ("Don't Stop Believin'", "Dont Stop Believin"),
        ("Another Brick In The Wall, Pt. 2", "Another Brick in the Wall (Part II)"),
        ("Lithium", "Lithium - 2011 Remaster"),
    ],
)
def test_the_same_base_title_joins(first: str, second: str) -> None:
    """Version text stripped, normalized, the part number spelled one way, punctuation gone."""
    works = [
        facts("w-1", title_key(first), artists=("a",)),
        facts("w-2", title_key(second), artists=("a",)),
    ]

    assert title_key(first) == title_key(second) != ""
    assert len(plan(works)) == 1


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Sweet Child O Mine", "Sweet Child Of Mine"),  # grouping's fuzzy match: 96
        ("Black Hole Sun", "Black Hole Suns"),  # 98
        ("Another Brick In The Wall, Part 1", "Another Brick In The Wall, Part 2"),
    ],
)
def test_a_fuzzy_only_pair_is_not_grouped(first: str, second: str) -> None:
    """D29: grouping would put these on one work by its fuzzy score; the planner never merges
    them."""
    works = [
        facts("w-1", title_key(first), artists=("a",)),
        facts("w-2", title_key(second), artists=("a",)),
    ]

    assert title_key(first) != title_key(second)
    assert plan(works) == []
