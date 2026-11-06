"""The catalog linker never promotes a name holding several MBIDs (artist model phase 2a, SPEC S9;
C41, M33; AUD-R026, AUD-R041).

The name's MBIDs are S6's reading of the tags: the lowercased, well-formed, non-placeholder values
holding exactly one MBID. When they are several, a decision that would be LINKED records AMBIGUOUS
instead: the name stays name-only and waits for phase 5's Artist decisions page. The lookups run
as today (the locked ``test_artist_linking_service.py`` counts them). Every other decision stays
what it was, and a value joining several MBIDs still links its member with the artist's name.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.domain.catalog import Artist, ArtistLinkCandidate, LinkDecision, LinkEvidence
from backend.domain.enums import ArtistLinkOutcome
from backend.services.artist_linking_service import decide_link, link_artist
from backend.services.normalization import normalize_artist
from tests.fakes.artist_linking import FakeArtistLinkingRepository
from tests.fakes.mb_client import FakeMbClient

UNKNOWN = "125ec42a-7229-4250-afc5-e057484327fe"  # MusicBrainz's [unknown]
AMBIGUOUS = LinkDecision(ArtistLinkOutcome.AMBIGUOUS)


def _mbid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _answer(n: int, name: str, *, survivor: int | None = None) -> dict[str, Any]:
    return {"id": _mbid(n if survivor is None else survivor), "name": name, "sort-name": name}


def _tags(*values: str) -> LinkEvidence:
    return LinkEvidence(tag_counts=tuple((value, 1) for value in values))


def _lookups(*numbers: int) -> list[str]:
    return [f"lookup_artist:{_mbid(n)}" for n in numbers]


def _linked_to(n: int, name: str) -> LinkDecision:
    candidate = ArtistLinkCandidate(mbid=_mbid(n), name=name, sort_name=name)
    return LinkDecision(ArtistLinkOutcome.LINKED, candidate)


def _decide(name: str, mb: FakeMbClient, evidence: LinkEvidence) -> LinkDecision:
    return decide_link(normalize_artist(name), evidence, mb)


# --- A name holding several MBIDs ----------------------------------------------------------------


def test_a_decision_that_would_link_is_ambiguous_and_the_lookups_are_unchanged() -> None:
    """One of the name's two MBIDs is named as the artist. Today that links it."""
    mb = FakeMbClient(artists={_mbid(1): _answer(1, "Prodigy"), _mbid(2): _answer(2, "Mobb Deep")})

    assert _decide("Prodigy", mb, _tags(_mbid(1), _mbid(2))) == AMBIGUOUS
    assert mb.calls == _lookups(1, 2)


def test_both_named_as_the_artist_stays_ambiguous() -> None:
    mb = FakeMbClient(artists={_mbid(1): _answer(1, "Prodigy"), _mbid(2): _answer(2, "Prodigy")})

    assert _decide("Prodigy", mb, _tags(_mbid(1), _mbid(2))) == AMBIGUOUS


def test_an_mbid_past_the_lookup_cap_still_makes_the_name_shared() -> None:
    """Only three MBIDs are looked up, and all four count."""
    answers = {_mbid(n): _answer(n, f"Act {n}") for n in (2, 3, 4)}
    mb = FakeMbClient(artists={**answers, _mbid(1): _answer(1, "Prodigy")})

    assert _decide("Prodigy", mb, _tags(*[_mbid(n) for n in (1, 2, 3, 4)])) == AMBIGUOUS
    assert mb.calls == _lookups(1, 2, 3)


def test_the_single_values_count_though_none_of_them_is_looked_up() -> None:
    """A joined value's members fill the three lookups, and one names the artist. The name still
    holds the two single values, so the decision is ambiguous."""
    answers = {_mbid(n): _answer(n, f"Act {n}") for n in (2, 3)}
    mb = FakeMbClient(artists={**answers, _mbid(1): _answer(1, "Prodigy")})
    joined = f"{_mbid(1)}, {_mbid(2)}, {_mbid(3)}"

    assert _decide("Prodigy", mb, _tags(joined, _mbid(4), _mbid(5))) == AMBIGUOUS
    assert mb.calls == _lookups(1, 2, 3)


def test_a_merged_answer_is_still_a_name_holding_two_tags() -> None:
    """The tags hold two MBIDs and MusicBrainz answers both with one survivor. The tags decide."""
    mb = FakeMbClient(
        artists={
            _mbid(1): _answer(1, "Prodigy", survivor=3),
            _mbid(2): _answer(2, "Prodigy", survivor=3),
        }
    )

    assert _decide("Prodigy", mb, _tags(_mbid(1), _mbid(2))) == AMBIGUOUS


def test_several_mbids_naming_someone_else_stay_a_mismatch() -> None:
    """Only a would-be link changes."""
    mb = FakeMbClient(artists={_mbid(1): _answer(1, "Mobb Deep"), _mbid(2): _answer(2, "Havoc")})

    decision = _decide("Prodigy", mb, _tags(_mbid(1), _mbid(2)))

    assert decision == LinkDecision(ArtistLinkOutcome.TAG_MISMATCH)


def test_link_artist_records_ambiguous_and_the_artist_stays_local() -> None:
    repo = FakeArtistLinkingRepository()
    artist = repo.add(
        Artist(
            id=str(uuid4()),
            name="Prodigy",
            sort_name="Prodigy",
            normalized_name="prodigy",
            needs_enhancement=False,
        ),
        evidence=_tags(_mbid(1), _mbid(2)),
    )
    mb = FakeMbClient(artists={_mbid(1): _answer(1, "Prodigy"), _mbid(2): _answer(2, "Mobb Deep")})

    assert link_artist(artist, repo, mb) == ArtistLinkOutcome.AMBIGUOUS

    assert (artist.mbid, artist.mb_lookup_outcome) == (None, ArtistLinkOutcome.AMBIGUOUS)


# --- A name holding at most one MBID: as today ---------------------------------------------------


def test_a_joined_value_still_links_its_member() -> None:
    mb = FakeMbClient(artists={_mbid(5): _answer(5, "Santana"), _mbid(6): _answer(6, "Rob Thomas")})

    assert _decide("Santana", mb, _tags(f"{_mbid(5)}, {_mbid(6)}")) == _linked_to(5, "Santana")


def test_a_joined_value_beside_one_single_value_is_one_mbid() -> None:
    mb = FakeMbClient(artists={_mbid(5): _answer(5, "Santana"), _mbid(6): _answer(6, "Rob Thomas")})

    assert _decide("Santana", mb, _tags(f"{_mbid(5)}, {_mbid(6)}", _mbid(5))) == _linked_to(
        5, "Santana"
    )


def test_a_joined_member_links_though_another_single_value_is_someone_else() -> None:
    """One single value is at most one MBID, so the member with the artist's name links."""
    mb = FakeMbClient(
        artists={
            _mbid(5): _answer(5, "Santana"),
            _mbid(6): _answer(6, "Rob Thomas"),
            _mbid(7): _answer(7, "Sting"),
        }
    )

    assert _decide("Santana", mb, _tags(f"{_mbid(5)}, {_mbid(6)}", _mbid(7))) == _linked_to(
        5, "Santana"
    )


def test_a_placeholder_beside_one_real_mbid_is_one_mbid() -> None:
    mb = FakeMbClient(artists={_mbid(1): _answer(1, "Prodigy")})

    assert _decide("Prodigy", mb, _tags(UNKNOWN, _mbid(1))) == _linked_to(1, "Prodigy")


def test_a_malformed_value_beside_one_real_mbid_is_one_mbid() -> None:
    mb = FakeMbClient(artists={_mbid(1): _answer(1, "Prodigy")})

    assert _decide("Prodigy", mb, _tags("artist-mbid-0001", _mbid(1))) == _linked_to(1, "Prodigy")


def test_one_mbid_in_two_cases_is_one_mbid() -> None:
    upper = "ABCDEF00-0000-4000-8000-000000000001"
    lower = upper.lower()
    mb = FakeMbClient(artists={lower: {"id": lower, "name": "Prodigy", "sort-name": "Prodigy"}})

    decision = _decide("Prodigy", mb, _tags(upper, lower))

    assert decision == LinkDecision(
        ArtistLinkOutcome.LINKED,
        ArtistLinkCandidate(mbid=lower, name="Prodigy", sort_name="Prodigy"),
    )
