"""``choose_performance``: the line's credit picks a performance inside its work (performances
phase 3a PR 3, SPEC S6, D3, D8, D9; design C47, M21, C16).

A pure function of the log name's linked artists, the work's performances with their credit
artists, the work's default performance (its master's) and the work's artist. Every artist is
compared as its merge-group primary (2a's Merge; ``primary_of``, absent = itself), so the
artists are sets.

D9 (Lance, 2026-11-12: "Yes, as written"): a performance answers better when it misses fewer of
the line's artists, then credits fewer others. One that misses none is a ``credit`` answer; one
that misses some but shares an artist is a ``shared_artist`` answer (M21); when none shares an
artist the answer is ``no_performer``. The default wins whenever it answers as well as the best
(strictly better is needed to leave it). Two or more non-default performances tied at the best
give no performance: their ids come back as ``tied`` for Lance's per-line pick. A log name with
no linked artist gets the default (C16).

D8 (Lance, 2026-11-12: "A3: work's artist"): a default with no credit (an untagged master, or
only placeholders) counts as crediting its work's artist (``work_artist``, by its primary), and
D9 then applies unchanged. Only the default does: another performance with no credit shares no
artist and is never chosen. With no work artist given, an uncredited default credits no one.
"""

from __future__ import annotations

import random
from uuid import UUID

from backend.domain.performances import (
    CreditedPerformance,
    PerformanceChoice,
    PerformanceReason,
    choose_performance,
)

BON_JOVI = "artist-bon-jovi"
NETTLES = "artist-jennifer-nettles"
KID_ROCK = "artist-kid-rock"
SAMBORA = "artist-richie-sambora"

SOLO = UUID("00000000-0000-4000-8000-00000000a001")
DUET = UUID("00000000-0000-4000-8000-00000000a002")
LIVE = UUID("00000000-0000-4000-8000-00000000a003")
TRIO = UUID("00000000-0000-4000-8000-00000000a004")
DUET_TOO = UUID("00000000-0000-4000-8000-00000000a005")
UNTAGGED = UUID("00000000-0000-4000-8000-00000000a006")


def credited(performance_id: UUID, *artists: str) -> CreditedPerformance:
    return CreditedPerformance(id=performance_id, artists=frozenset(artists))


WORK = [credited(SOLO, BON_JOVI), credited(DUET, BON_JOVI, NETTLES)]
"""Who Says You Can't Go Home: the solo master's performance and the duet."""


def choice(reason: PerformanceReason, performance_id: UUID | None) -> PerformanceChoice:
    return PerformanceChoice(reason=reason, performance_id=performance_id, tied=())


def test_the_reasons() -> None:
    assert [r.value for r in PerformanceReason] == [
        "credit",
        "shared_artist",
        "default",
        "no_performer",
    ]


def test_a_duet_line_picks_the_duet() -> None:
    """ "BON JOVI/JENNIFER NETTLES" plays the duet, not the solo master."""
    picked = choose_performance({BON_JOVI, NETTLES}, WORK, SOLO)

    assert picked == choice(PerformanceReason.CREDIT, DUET)


def test_a_solo_line_keeps_the_default() -> None:
    assert choose_performance({BON_JOVI}, WORK, SOLO) == choice(PerformanceReason.DEFAULT, SOLO)


def test_the_duet_as_default_answers_its_own_line() -> None:
    """When the duet is the master's performance, the duet line keeps the default."""
    assert choose_performance({BON_JOVI, NETTLES}, WORK, DUET) == choice(
        PerformanceReason.DEFAULT, DUET
    )


def test_a_live_performance_with_the_same_credit_never_beats_the_default() -> None:
    """The credit can't tell a live take from the studio one; that is a version entry's job."""
    work = [credited(SOLO, BON_JOVI), credited(LIVE, BON_JOVI)]

    assert choose_performance({BON_JOVI}, work, SOLO) == choice(PerformanceReason.DEFAULT, SOLO)


def test_of_two_candidates_the_closer_wins() -> None:
    """The duet credits exactly the line's two; the trio credits one more."""
    work = [*WORK, credited(TRIO, BON_JOVI, NETTLES, SAMBORA)]

    assert choose_performance({BON_JOVI, NETTLES}, work, SOLO) == choice(
        PerformanceReason.CREDIT, DUET
    )


def test_a_tie_chooses_none_and_names_them() -> None:
    """Two duet performances answer equally: Lance picks (D3); the order given doesn't matter."""
    work = [*WORK, credited(DUET_TOO, NETTLES, BON_JOVI)]

    for performances in (work, list(reversed(work))):
        picked = choose_performance({BON_JOVI, NETTLES}, performances, SOLO)
        assert picked == PerformanceChoice(
            reason=PerformanceReason.CREDIT, performance_id=None, tied=(DUET, DUET_TOO)
        )


def test_a_tie_with_the_default_keeps_the_default() -> None:
    work = [*WORK, credited(DUET_TOO, BON_JOVI, NETTLES)]

    assert choose_performance({BON_JOVI, NETTLES}, work, DUET) == choice(
        PerformanceReason.DEFAULT, DUET
    )


def test_a_performer_credited_only_on_the_duet() -> None:
    """ "JENNIFER NETTLES" alone: she is credited on the duet, so it answers her credit."""
    assert choose_performance({NETTLES}, WORK, SOLO) == choice(PerformanceReason.CREDIT, DUET)


def test_shared_artist_when_no_performance_credits_them_all() -> None:
    """M21: "JENNIFER NETTLES/KID ROCK" has no performance of its own; the duet shares one."""
    assert choose_performance({NETTLES, KID_ROCK}, WORK, SOLO) == choice(
        PerformanceReason.SHARED_ARTIST, DUET
    )


def test_a_shared_artist_tie_with_the_default_keeps_it() -> None:
    """ "BON JOVI/KID ROCK": the solo misses one and credits none else; the duet misses one and
    credits one else, so the default answers best."""
    assert choose_performance({BON_JOVI, KID_ROCK}, WORK, SOLO) == choice(
        PerformanceReason.DEFAULT, SOLO
    )


def test_no_performer() -> None:
    """M21: never an unrelated performance; the play counts as missing."""
    assert choose_performance({KID_ROCK}, WORK, SOLO) == choice(
        PerformanceReason.NO_PERFORMER, None
    )


def test_no_linked_artist_gets_the_default() -> None:
    """C16, also when the work has no default performance."""
    assert choose_performance(set(), WORK, SOLO) == choice(PerformanceReason.DEFAULT, SOLO)
    assert choose_performance(set(), WORK, None) == choice(PerformanceReason.DEFAULT, None)


def test_with_no_default_the_best_answer_is_chosen() -> None:
    """A work whose master has no performance has no default to beat."""
    assert choose_performance({BON_JOVI}, WORK, None) == choice(PerformanceReason.CREDIT, SOLO)
    assert choose_performance({KID_ROCK}, WORK, None) == choice(
        PerformanceReason.NO_PERFORMER, None
    )


def test_merged_artists_compare_as_their_primary() -> None:
    """2a's Merge: the line is linked to a member of the group the duet credits by another."""
    member, other_member, primary = "artist-nettles-stub", "artist-nettles-name", NETTLES
    primary_of = {member: primary, other_member: primary}
    work = [credited(SOLO, BON_JOVI), credited(DUET, BON_JOVI, other_member)]

    picked = choose_performance({BON_JOVI, member}, work, SOLO, primary_of=primary_of)
    unmerged = choose_performance({BON_JOVI, member}, work, SOLO)

    assert picked == choice(PerformanceReason.CREDIT, DUET)
    assert unmerged == choice(PerformanceReason.DEFAULT, SOLO)


def test_two_links_in_one_group_are_one_artist() -> None:
    """A log name linked to both a member and its primary names one artist: a solo line."""
    primary_of = {"artist-bon-jovi-stub": BON_JOVI}

    picked = choose_performance(
        {BON_JOVI, "artist-bon-jovi-stub"}, WORK, SOLO, primary_of=primary_of
    )

    assert picked == choice(PerformanceReason.DEFAULT, SOLO)


# --- D8: an untagged master credits its work's artist -----------------------------------------

UNTAGGED_WORK = [
    credited(UNTAGGED),
    credited(SOLO, BON_JOVI),
    credited(DUET, BON_JOVI, NETTLES),
]
"""The same song with an untagged master: a tagged solo copy and the tagged duet."""


def on_untagged(
    line: set[str], work: list[CreditedPerformance] = UNTAGGED_WORK
) -> PerformanceChoice:
    """The answer for *line* on Bon Jovi's work whose default is the untagged master."""
    return choose_performance(line, work, UNTAGGED, work_artist=BON_JOVI)


def test_an_untagged_master_keeps_its_solo_lines() -> None:
    """It credits Bon Jovi, so it ties the tagged solo and live copies, and the default wins."""
    work = [*UNTAGGED_WORK, credited(LIVE, BON_JOVI)]

    assert on_untagged({BON_JOVI}, work) == choice(PerformanceReason.DEFAULT, UNTAGGED)


def test_an_untagged_master_keeps_a_solo_line_over_the_duet() -> None:
    work = [credited(UNTAGGED), credited(DUET, BON_JOVI, NETTLES)]

    assert on_untagged({BON_JOVI}, work) == choice(PerformanceReason.DEFAULT, UNTAGGED)


def test_an_untagged_master_gives_way_to_the_duet() -> None:
    assert on_untagged({BON_JOVI, NETTLES}) == choice(PerformanceReason.CREDIT, DUET)


def test_an_untagged_master_gives_way_for_another_performer() -> None:
    """ "JENNIFER NETTLES" alone gets the duet she is credited on; a Kid Rock line gets his own
    performance of the song (M21), as with a tagged master."""
    work = [*UNTAGGED_WORK, credited(LIVE, KID_ROCK)]

    assert on_untagged({NETTLES}, work) == choice(PerformanceReason.CREDIT, DUET)
    assert on_untagged({KID_ROCK}, work) == choice(PerformanceReason.CREDIT, LIVE)


def test_an_untagged_master_and_a_shared_artist() -> None:
    """ "BON JOVI/KID ROCK": the master and the solo both miss one and credit none else, so the
    default wins; "JENNIFER NETTLES/KID ROCK" shares Nettles with the duet only."""
    assert on_untagged({BON_JOVI, KID_ROCK}) == choice(PerformanceReason.DEFAULT, UNTAGGED)
    assert on_untagged({NETTLES, KID_ROCK}) == choice(PerformanceReason.SHARED_ARTIST, DUET)


def test_an_untagged_master_gives_way_to_the_closest_and_ties_to_lance() -> None:
    work = [
        credited(UNTAGGED),
        credited(DUET, BON_JOVI, NETTLES),
        credited(TRIO, BON_JOVI, NETTLES, SAMBORA),
    ]
    tied = [*work, credited(DUET_TOO, BON_JOVI, NETTLES)]

    assert on_untagged({BON_JOVI, NETTLES}, work) == choice(PerformanceReason.CREDIT, DUET)
    assert on_untagged({BON_JOVI, NETTLES}, tied) == PerformanceChoice(
        reason=PerformanceReason.CREDIT, performance_id=None, tied=(DUET, DUET_TOO)
    )


def test_the_work_artist_counts_as_its_primary() -> None:
    """The work's artist is a name-only row merged into the Bon Jovi the line links to."""
    name_only = "artist-bon-jovi-name-only"
    work = [credited(UNTAGGED), credited(SOLO, BON_JOVI)]

    merged = choose_performance(
        {BON_JOVI}, work, UNTAGGED, work_artist=name_only, primary_of={name_only: BON_JOVI}
    )
    unmerged = choose_performance({BON_JOVI}, work, UNTAGGED, work_artist=name_only)

    assert merged == choice(PerformanceReason.DEFAULT, UNTAGGED)
    assert unmerged == choice(PerformanceReason.CREDIT, SOLO)


def test_with_no_work_artist_an_untagged_master_credits_no_one() -> None:
    work = [credited(UNTAGGED), credited(SOLO, BON_JOVI)]

    assert choose_performance({BON_JOVI}, work, UNTAGGED) == choice(PerformanceReason.CREDIT, SOLO)


def test_a_credited_default_ignores_the_work_artist() -> None:
    """A master credited to Nettles alone stays credited to her, whatever the work's artist."""
    work = [credited(SOLO, NETTLES), credited(DUET, BON_JOVI, NETTLES)]

    picked = choose_performance({BON_JOVI}, work, SOLO, work_artist=BON_JOVI)

    assert picked == choice(PerformanceReason.CREDIT, DUET)


def test_only_the_default_borrows_the_work_artist() -> None:
    """Another performance with no credit shares no artist, so it is never chosen."""
    work = [credited(SOLO, BON_JOVI), credited(UNTAGGED)]

    assert choose_performance({KID_ROCK}, work, SOLO, work_artist=KID_ROCK) == choice(
        PerformanceReason.NO_PERFORMER, None
    )
    assert choose_performance({BON_JOVI}, [credited(UNTAGGED)], None, work_artist=BON_JOVI) == (
        choice(PerformanceReason.NO_PERFORMER, None)
    )


def test_it_reads_only_what_it_is_given() -> None:
    """Pure: the same answer every time, whatever order the performances come in."""
    work = [*WORK, credited(TRIO, BON_JOVI, NETTLES, SAMBORA), credited(LIVE, BON_JOVI)]
    shuffled = list(work)
    random.Random(7).shuffle(shuffled)

    first = choose_performance({BON_JOVI, NETTLES}, work, SOLO)

    assert choose_performance({BON_JOVI, NETTLES}, shuffled, SOLO) == first
    assert choose_performance(frozenset({NETTLES, BON_JOVI}), tuple(work), SOLO) == first
