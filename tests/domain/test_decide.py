"""decide(): one log name's evidence in, its link decisions out (artist model phase 4.2 PR 1,
SPEC S1, E3; D1; design C1, C2, C8, C9, C10, C51, C55, C67, M9, M10, M14; AUD-R036, AUD-R095).

``decide(evidence, thresholds)`` is pure. It links an act when two independent signals agree
(C1): the name and two counting titles, or one rare title; or the owned-song anchor (C67), where
the name covers every word and the library holds one of its played titles that counts, so the
name is itself the second signal and C9 doesn't apply. One signal is a suggestion. Titles held by
``common_acts`` or more distinct acts don't count (C8, M10). A title from MusicBrainz votes for
the credit holding most of its covering hits, by a lead of ``title_lead`` (D1); a title voting
for another credit sends both to review (D1, M14). A near-miss name needs two titles (C9) and
never adds a second artist (C10). Acts explaining different words of the name all link (M9,
M10); a performer of a linked credit who explains no word is suggested (C6). A name holding a
manual link is left alone (C18, E3). A name whose needed search isn't cached waits (E1).

The named cases (C51, and the SPEC's probes) are the JSON files in tests/fixtures/linking/named,
hand-built evidence each with its expected decisions; the harness runs the same files.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from backend.domain.enums import LinkKind, LinkSignal
from backend.domain.linking import (
    Candidate,
    Credit,
    Decision,
    LibraryTitle,
    LinkingThresholds,
    NameEvidence,
    NameFit,
    Performer,
    TitleVotes,
    decide,
    title_vote,
)

ROOT = Path(__file__).resolve().parents[2]
NAMED = ROOT / "tests" / "fixtures" / "linking" / "named"

# Passed explicitly: no case here pins a linking default (D1: title_lead comes from D4's count).
T = LinkingThresholds(title_lead=1, common_acts=3, shortlist=10, near_miss=0.85, name_max_len=30)

A = Performer("mbid:a", "Act A")
B = Performer("mbid:b", "Act B")
C = Performer("mbid:c", "Act C")


def cand(
    *performers: Performer,
    explained: tuple[int, ...] = (0,),
    exact: bool = False,
    fuzzy: bool = False,
    titles: tuple[tuple[str, int], ...] = (),
    bystanders: tuple[str, ...] = (),
) -> Candidate:
    fit = NameFit(
        explained=frozenset(explained),
        exact=exact,
        fuzzy=fuzzy,
        bystanders=frozenset(bystanders),
    )
    library = tuple(LibraryTitle(title, acts) for title, acts in titles)
    return Candidate(Credit(performers), fit, library)


def votes(
    title: str,
    *hits: tuple[tuple[Performer, ...], int],
    acts: int = 0,
    holder: str | None = None,
) -> TitleVotes:
    return TitleVotes(
        title=title,
        acts=acts,
        holder=holder,
        hits=tuple((Credit(performers), n) for performers, n in hits),
    )


def evidence(
    words: tuple[str, ...],
    *candidates: Candidate,
    musicbrainz: tuple[TitleVotes, ...] = (),
    manual: bool = False,
    waiting: bool = False,
) -> NameEvidence:
    return NameEvidence(
        name=" ".join(words).upper(),
        words=words,
        candidates=candidates,
        musicbrainz=musicbrainz,
        manual=manual,
        waiting=waiting,
    )


def outcome(decisions: list[Decision]) -> set[tuple[LinkKind, str | None]]:
    return {(d.kind, None if d.performer is None else d.performer.key) for d in decisions}


LINK, SUGGEST, REVIEW, WAIT = (
    LinkKind.LINK,
    LinkKind.SUGGEST,
    LinkKind.REVIEW,
    LinkKind.WAIT,
)


# --- the named cases (C51; the SPEC's probes) ----------------------------------------------------


def _performer(raw: dict[str, Any]) -> Performer:
    return Performer(raw["key"], raw["name"])


def _evidence_from(raw: dict[str, Any]) -> NameEvidence:
    """A fixture's evidence, built here from the JSON so this file needs only the domain."""
    candidates = tuple(
        Candidate(
            Credit(tuple(_performer(p) for p in c["performers"])),
            NameFit(
                explained=frozenset(c["fit"]["explained"]),
                exact=c["fit"]["exact"],
                fuzzy=c["fit"]["fuzzy"],
                leftovers=tuple(c["fit"]["leftovers"]),
                bystanders=frozenset(c["fit"]["bystanders"]),
            ),
            tuple(LibraryTitle(t["title"], t["acts"]) for t in c["library_titles"]),
        )
        for c in raw["candidates"]
    )
    musicbrainz = tuple(
        TitleVotes(
            title=v["title"],
            acts=v["acts"],
            holder=v["holder"],
            hits=tuple(
                (Credit(tuple(_performer(p) for p in h["performers"])), h["hits"])
                for h in v["hits"]
            ),
        )
        for v in raw["musicbrainz"]
    )
    return NameEvidence(
        name="",
        words=tuple(raw["words"]),
        candidates=candidates,
        musicbrainz=musicbrainz,
        manual=raw["manual"],
        waiting=raw["waiting"],
    )


NAMED_CASES = sorted(NAMED.glob("*.json"))


def test_every_named_case_is_present() -> None:
    names = {json.loads(path.read_text(encoding="utf-8"))["name"] for path in NAMED_CASES}
    assert names >= {
        "ARCHIES",
        "KAI",
        "CHICK",
        "ARCHITECTS UK",
        "ALICE COOPER",
        "BLACKSHEEP",
        "'N SYNC & GLORIA ESTEFAN",
        "O. NEWTON-JOHN/J. TRAVOLTA",
        "C. SIMON/J. TAYLOR",
        "SANFORD/TOWNSEND",
    }


@pytest.mark.parametrize("path", NAMED_CASES, ids=lambda path: path.stem)
def test_named_case(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    expected = {(LinkKind(e["kind"]), e["performer"]) for e in raw["expected"]}

    assert outcome(decide(_evidence_from(raw["evidence"]), T)) == expected


# --- C1: two independent signals link; one suggests ---------------------------------------------


def test_an_exact_name_alone_is_a_suggestion() -> None:
    """D5's example: today's stage links this; under C1 it is one signal."""
    assert outcome(decide(evidence(("act", "a"), cand(A, explained=(0, 1), exact=True)), T)) == {
        (SUGGEST, "mbid:a")
    }


def test_the_name_and_two_agreeing_titles_link() -> None:
    found = evidence(
        ("act", "a"),
        cand(A, explained=(0, 1), exact=True),
        musicbrainz=(votes("one", ((A,), 4)), votes("two", ((A,), 2))),
    )

    [decision] = decide(found, T)

    assert (decision.kind, decision.performer) == (LINK, A)
    assert LinkSignal.MUSICBRAINZ_TITLE in decision.signals


def test_two_library_titles_link_a_name_that_explains_some_of_its_words() -> None:
    """ARCHITECTS UK's shape: the unexplained word is reported, not a veto (C2, C24)."""
    found = evidence(("act", "x"), cand(A, titles=(("one", 1), ("two", 2))))

    assert outcome(decide(found, T)) == {(LINK, "mbid:a")}


def test_no_evidence_decides_nothing() -> None:
    assert decide(evidence(("act",)), T) == []


# --- the owned-song anchor (C67) ----------------------------------------------------------------


def test_the_anchor_links_on_one_played_title() -> None:
    """The name covers every word, so it is the second signal; C9 doesn't apply."""
    [decision] = decide(evidence(("act", "a"), cand(A, explained=(0, 1), titles=(("t", 2),))), T)

    assert (decision.kind, decision.performer, decision.signals) == (
        LINK,
        A,
        frozenset({LinkSignal.ANCHOR}),
    )


def test_the_anchor_needs_every_word_explained() -> None:
    """One word of two, one library title: no anchor, and one title is no second signal."""
    assert outcome(decide(evidence(("act", "a"), cand(A, titles=(("t", 2),))), T)) == {
        (SUGGEST, "mbid:a")
    }


def test_two_anchors_go_to_review() -> None:
    """C67: two or more acts → M14."""
    found = evidence(
        ("act",),
        cand(A, titles=(("t", 2),)),
        cand(B, titles=(("u", 2),)),
    )

    assert outcome(decide(found, T)) == {(REVIEW, "mbid:a"), (REVIEW, "mbid:b")}


def test_an_anchored_credit_links_every_performer_who_explains_a_word() -> None:
    found = evidence(("a", "b"), cand(A, B, explained=(0, 1), titles=(("duet", 2),)))

    assert outcome(decide(found, T)) == {(LINK, "mbid:a"), (LINK, "mbid:b")}


def test_a_credit_adding_a_bystander_to_the_same_act_is_no_rival() -> None:
    """AEROSMITH: the library holds "walk this way" by Aerosmith and by Run-D.M.C. feat.
    Aerosmith. Both credits' explaining performer is Aerosmith, so they don't conflict."""
    found = evidence(
        ("aerosmith",),
        cand(A, titles=(("walk this way", 2),)),
        cand(B, A, titles=(("walk this way", 2),), bystanders=("mbid:b",)),
    )

    assert outcome(decide(found, T)) == {(LINK, "mbid:a"), (SUGGEST, "mbid:b")}


def test_a_performer_who_explains_no_word_is_suggested() -> None:
    """C6: "SANTANA" and a credit of Santana feat. Rob Thomas: Rob Thomas is only suggested."""
    found = evidence(
        ("a",),
        cand(A, B, explained=(0,), titles=(("smooth", 1),), bystanders=("mbid:b",)),
    )

    assert outcome(decide(found, T)) == {(LINK, "mbid:a"), (SUGGEST, "mbid:b")}


# --- titles: rarity, common titles, D1's votes --------------------------------------------------


def test_common_titles_do_not_count() -> None:
    """M10, C8: titles 3 or more distinct acts hold are no evidence."""
    found = evidence(("act",), cand(A, titles=(("silent night", 13), ("home", 18))))

    assert LINK not in {d.kind for d in decide(found, T)}


def test_the_common_bar_is_the_threshold() -> None:
    found = evidence(("act",), cand(A, titles=(("one", 3), ("two", 3))))
    looser = LinkingThresholds(
        title_lead=1, common_acts=4, shortlist=10, near_miss=0.85, name_max_len=30
    )

    assert outcome(decide(found, T)) == {(SUGGEST, "mbid:a")}
    assert outcome(decide(found, looser)) == {(LINK, "mbid:a")}


def test_one_rare_library_title_links_a_name_that_explains_its_words() -> None:
    """M10: one rare title; C8: rare is one distinct act."""
    found = evidence(("act", "a", "x"), cand(A, explained=(0, 1), titles=(("only ours", 1),)))

    assert outcome(decide(found, T)) == {(LINK, "mbid:a")}


def test_a_title_votes_for_the_credit_with_most_covering_hits() -> None:
    assert title_vote(votes("t", ((A,), 3), ((B,), 1)), T) == Credit((A,))


def test_a_tie_votes_for_nobody() -> None:
    assert title_vote(votes("t", ((A,), 2), ((B,), 2)), T) is None


def test_the_lead_is_the_threshold() -> None:
    """D1: a lead of title_lead hits over the runner-up; scores are never read."""
    split = votes("t", ((A,), 3), ((B,), 2))
    lead_2 = LinkingThresholds(
        title_lead=2, common_acts=3, shortlist=10, near_miss=0.85, name_max_len=30
    )

    assert title_vote(split, T) == Credit((A,))
    assert title_vote(split, lead_2) is None
    assert title_vote(votes("t", ((A,), 2)), lead_2) == Credit((A,))


def test_one_musicbrainz_title_is_a_suggestion() -> None:
    found = evidence(("act",), cand(A, exact=True), musicbrainz=(votes("t", ((A,), 5)),))

    assert outcome(decide(found, T)) == {(SUGGEST, "mbid:a")}


def test_one_rare_musicbrainz_title_links() -> None:
    """D1: its local count is exactly 1, held by the same credit, and its covering hits hold
    that credit alone."""
    rare = votes("t", ((A,), 5), acts=1, holder="mbid:a")

    assert outcome(decide(evidence(("act",), cand(A), musicbrainz=(rare,)), T)) == {
        (LINK, "mbid:a")
    }


@pytest.mark.parametrize(
    "title",
    [
        pytest.param(votes("t", ((A,), 5), acts=0), id="count-0-is-unknown-not-rare"),
        pytest.param(votes("t", ((A,), 5), acts=1, holder="mbid:b"), id="held-by-another-act"),
        pytest.param(
            votes("t", ((A,), 5), ((B,), 1), acts=1, holder="mbid:a"), id="hits-hold-two-credits"
        ),
    ],
)
def test_a_single_musicbrainz_title_that_is_not_rare_only_suggests(title: TitleVotes) -> None:
    found = evidence(("act",), cand(A), musicbrainz=(title,))

    assert LINK not in {d.kind for d in decide(found, T)}


def test_a_title_voting_for_another_credit_sends_both_to_review() -> None:
    """D1: two titles for A and one for B; the tribute act doesn't outvote, it blocks."""
    found = evidence(
        ("creedence", "clearwater"),
        cand(A, explained=(0, 1)),
        cand(B, explained=(0, 1)),
        musicbrainz=(
            votes("bad moon rising", ((A,), 9), ((B,), 1)),
            votes("proud mary", ((A,), 6)),
            votes("have you ever seen the rain", ((B,), 3), ((A,), 1)),
        ),
    )

    assert outcome(decide(found, T)) == {(REVIEW, "mbid:a"), (REVIEW, "mbid:b")}


def test_a_name_only_rival_does_not_block_a_link() -> None:
    """M14's clear winner: the rival has the name but no title."""
    found = evidence(
        ("act",),
        cand(A, exact=True),
        cand(B, exact=True),
        musicbrainz=(votes("one", ((A,), 4)), votes("two", ((A,), 3))),
    )

    assert outcome(decide(found, T)) == {(LINK, "mbid:a"), (SUGGEST, "mbid:b")}


# --- near-miss names (C9, C10) ------------------------------------------------------------------


def test_a_near_miss_name_and_one_title_is_circular() -> None:
    """C9: the one title is the song the link would then match."""
    found = evidence(("archies",), cand(A, fuzzy=True, titles=(("only ours", 1),)))

    assert outcome(decide(found, T)) == {(SUGGEST, "mbid:a")}


def test_a_near_miss_name_with_two_titles_links() -> None:
    found = evidence(("skynyrd",), cand(A, fuzzy=True, titles=(("one", 1), ("two", 2))))

    assert outcome(decide(found, T)) == {(LINK, "mbid:a")}


def test_a_near_miss_name_never_adds_a_second_artist() -> None:
    """C10: A explains word 0 and links; B, a near miss for word 1, is only suggested."""
    found = evidence(
        ("a", "b"),
        cand(A, explained=(0,), titles=(("one", 1), ("two", 1))),
        cand(B, explained=(1,), fuzzy=True, titles=(("three", 1), ("four", 1))),
    )

    assert outcome(decide(found, T)) == {(LINK, "mbid:a"), (SUGGEST, "mbid:b")}


# --- several artists (M9, M10) ------------------------------------------------------------------


def test_acts_explaining_different_words_all_link() -> None:
    """M9: a log name may link to several artists; each needs two titles or one rare (M10)."""
    found = evidence(
        ("a", "b"),
        cand(A, explained=(0,), titles=(("one", 1), ("two", 2))),
        cand(B, explained=(1,), titles=(("three", 1),)),
    )

    assert outcome(decide(found, T)) == {(LINK, "mbid:a"), (LINK, "mbid:b")}


def test_an_extra_artist_with_only_a_common_title_is_no_candidate() -> None:
    """M10: the common title doesn't count, and a name that explains only some words and holds
    no counting title is not even suggested (SANFORD/TOWNSEND's Devin Townsend)."""
    found = evidence(
        ("a", "b"),
        cand(A, explained=(0,), titles=(("one", 1), ("two", 2))),
        cand(B, explained=(1,), titles=(("home", 18),)),
    )

    assert outcome(decide(found, T)) == {(LINK, "mbid:a")}


def test_a_common_title_never_anchors() -> None:
    """A word may be the start of a longer word (C67), so a common title would let "KAI"
    anchor to Kaiser Chiefs through "Home"; the anchor needs a title that counts."""
    found = evidence(("kai",), cand(A, titles=(("home", 18),)))

    assert outcome(decide(found, T)) == {(SUGGEST, "mbid:a")}


# --- manual links, waiting, purity ---------------------------------------------------------------


def test_manual_untouched() -> None:
    """E3, C18: a name holding a manual link gets no decision; 4.4 completes its performers."""
    found = evidence(("act",), cand(A, titles=(("t", 1),)), manual=True)

    assert decide(found, T) == []


def test_a_name_whose_search_is_not_cached_waits() -> None:
    """E1: nothing links locally and a needed search isn't cached."""
    found = evidence(("act",), cand(A, exact=True), waiting=True)

    assert outcome(decide(found, T)) == {(WAIT, None), (SUGGEST, "mbid:a")}


def test_a_local_link_does_not_wait() -> None:
    found = evidence(("act",), cand(A, titles=(("t", 2),)), waiting=True)

    assert outcome(decide(found, T)) == {(LINK, "mbid:a")}


def test_a_performer_gets_one_decision_the_strongest() -> None:
    """A appears alone and inside a linked credit: it is linked once."""
    found = evidence(
        ("a", "b"),
        cand(A, B, explained=(0, 1), titles=(("duet", 2),)),
        cand(A, explained=(0,), exact=True),
    )

    assert outcome(decide(found, T)) == {(LINK, "mbid:a"), (LINK, "mbid:b")}


def test_decide_is_pure() -> None:
    found = evidence(
        ("act",),
        cand(A, titles=(("t", 2),)),
        cand(B, exact=True),
        musicbrainz=(votes("u", ((C,), 3)),),
    )

    first, second = decide(found, T), decide(found, T)

    assert first == second
    assert first is not second


def test_the_domain_types_are_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        T.title_lead = 5  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        evidence(("act",)).manual = True  # type: ignore[misc]


def test_the_domain_contract_forbids_rapidfuzz_and_pydantic() -> None:
    """C55: decide() stays pure. `uv run lint-imports` enforces the contract."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    [contract] = [
        c
        for c in config["tool"]["importlinter"]["contracts"]
        if c.get("source_modules") == ["backend.domain"]
    ]

    assert {"rapidfuzz", "pydantic"} <= set(contract["forbidden_modules"])
