"""Acceptance tests: the local-artist linker's rule, tags only (AUD-R026; spec 2026-10-05 D15).

A wrong MBID is worse than none: song matching trusts it (Steps A and B). So a local artist
links only to an MBID its own present files carry (Lance, 2026-10-08: tags only):
- split each tag value on "," ";" "/", keep the well-formed MBIDs, rank them by present files
  (summed per MBID), and drop MusicBrainz's special-purpose artists (SPECIAL_PURPOSE_MBIDS);
- look up at most 3, and link the one whose MusicBrainz name normalizes (normalize_artist) to
  the artist's normalized name. None: tag_mismatch (a collaboration's member, an alias).
  Several: ambiguous. Only placeholders (listed, or a bracketed MusicBrainz name):
  special_purpose;
- no well-formed tag, or an empty normalized name: no_evidence, without asking MusicBrainz.
  There is no name search (deferred, D15b).
link_artist writes the decision. A link the repository refuses (another artist holds the MBID)
is recorded as duplicate.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from backend.domain.catalog import (
    Artist,
    ArtistLinkCandidate,
    InvalidLinkDecisionError,
    InvalidMusicBrainzIdError,
    LinkDecision,
    LinkEvidence,
)
from backend.domain.enums import ArtistLinkOutcome, CatalogSource
from backend.services.artist_linking_service import (
    SPECIAL_PURPOSE_MBIDS,
    decide_link,
    link_artist,
    tag_mbids,
)
from backend.services.normalization import normalize_artist
from tests.fakes.artist_linking import FakeArtistLinkingRepository
from tests.fakes.mb_client import FakeMbClient

LINKED = ArtistLinkOutcome.LINKED
UNKNOWN = "125ec42a-7229-4250-afc5-e057484327fe"  # MusicBrainz's [unknown]
VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"  # MusicBrainz's Various Artists


def _mbid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _answer(
    n: int,
    name: str,
    *,
    survivor: int | None = None,
    sort_name: str | None = None,
    disambiguation: str | None = None,
) -> dict[str, Any]:
    """One lookup_artist answer. A merged MBID answers with its survivor's id."""
    data: dict[str, Any] = {
        "id": _mbid(n if survivor is None else survivor),
        "name": name,
        "sort-name": sort_name or name,
    }
    if disambiguation is not None:
        data["disambiguation"] = disambiguation
    return data


def _tags(*values: str, files: int = 1) -> LinkEvidence:
    return LinkEvidence(tag_counts=tuple((value, files) for value in values))


def _decide(name: str, mb: FakeMbClient, evidence: LinkEvidence) -> LinkDecision:
    return decide_link(normalize_artist(name), evidence, mb)


def _linked_to(
    n: int, name: str, *, sort_name: str | None = None, disambiguation: str | None = None
) -> LinkDecision:
    candidate = ArtistLinkCandidate(
        mbid=_mbid(n), name=name, sort_name=sort_name or name, disambiguation=disambiguation
    )
    return LinkDecision(LINKED, candidate)


def _local(name: str) -> Artist:
    return Artist(
        id=str(uuid4()),
        name=name,
        sort_name=name,
        normalized_name=normalize_artist(name),
        needs_enhancement=False,
    )


# --- Linking ---------------------------------------------------------------------------------


def test_a_tag_naming_the_same_artist_links() -> None:
    mb = FakeMbClient(artists={_mbid(1): _answer(1, "Nirvana", disambiguation="90s US grunge")})

    decision = _decide("NIRVANA", mb, _tags(_mbid(1)))

    assert decision == _linked_to(1, "Nirvana", disambiguation="90s US grunge")
    assert mb.calls == [f"lookup_artist:{_mbid(1)}"]


def test_the_name_must_normalize_the_same_not_match_exactly() -> None:
    mb = FakeMbClient(artists={_mbid(2): _answer(2, "Guns N’ Roses")})

    assert _decide("GUNS N' ROSES", mb, _tags(_mbid(2))) == _linked_to(2, "Guns N’ Roses")


def test_a_merged_tag_mbid_links_the_surviving_artist() -> None:
    mb = FakeMbClient(
        artists={_mbid(3): _answer(3, "Boz Scaggs", survivor=4, sort_name="Scaggs, Boz")}
    )

    decision = _decide("Boz Scaggs", mb, _tags(_mbid(3)))

    assert decision == _linked_to(4, "Boz Scaggs", sort_name="Scaggs, Boz")


def test_a_multi_valued_tag_links_the_member_with_the_artists_name() -> None:
    mb = FakeMbClient(artists={_mbid(5): _answer(5, "Santana"), _mbid(6): _answer(6, "Rob Thomas")})

    decision = _decide("Santana", mb, _tags(f"{_mbid(5)}, {_mbid(6)}"))

    assert decision == _linked_to(5, "Santana")


# --- Not linking -----------------------------------------------------------------------------


def test_a_tag_naming_someone_else_is_a_mismatch() -> None:
    # Dev DB: the "Ozzy" files carry Ozzy Osbourne's MBID. There is no name search to fall to.
    mb = FakeMbClient(artists={_mbid(7): _answer(7, "Ozzy Osbourne")})

    decision = _decide("Ozzy", mb, _tags(_mbid(7)))

    assert decision == LinkDecision(ArtistLinkOutcome.TAG_MISMATCH)
    assert mb.calls == [f"lookup_artist:{_mbid(7)}"]


def test_a_collaboration_tagged_with_both_members_is_a_mismatch() -> None:
    mb = FakeMbClient(artists={_mbid(8): _answer(8, "U2"), _mbid(9): _answer(9, "Bob Dylan")})

    decision = _decide("U2 & Bob Dylan", mb, _tags(f"{_mbid(8)}, {_mbid(9)}"))

    assert decision == LinkDecision(ArtistLinkOutcome.TAG_MISMATCH)
    assert mb.calls == [f"lookup_artist:{_mbid(8)}", f"lookup_artist:{_mbid(9)}"]


def test_a_tag_musicbrainz_does_not_know_is_a_mismatch() -> None:
    mb = FakeMbClient()  # lookup_artist answers None, as for a 404

    assert _decide("Men Without Hats", mb, _tags(_mbid(10))) == LinkDecision(
        ArtistLinkOutcome.TAG_MISMATCH
    )


def test_a_malformed_id_in_musicbrainzs_answer_is_skipped() -> None:
    mb = FakeMbClient(artists={_mbid(11): {"id": "not-an-mbid", "name": "Nirvana"}})

    assert _decide("Nirvana", mb, _tags(_mbid(11))) == LinkDecision(ArtistLinkOutcome.TAG_MISMATCH)


def test_two_tagged_artists_with_the_same_name_are_ambiguous() -> None:
    mb = FakeMbClient(artists={_mbid(12): _answer(12, "Boston"), _mbid(13): _answer(13, "Boston")})

    assert _decide("Boston", mb, _tags(_mbid(12), _mbid(13))) == LinkDecision(
        ArtistLinkOutcome.AMBIGUOUS
    )


def test_at_most_three_tagged_mbids_are_looked_up_most_files_first() -> None:
    mb = FakeMbClient()
    evidence = LinkEvidence(
        tag_counts=(
            (_mbid(14), 1),
            (_mbid(15), 5),
            (f"{_mbid(16)}, {_mbid(14)}", 3),  # 14 totals 4 files over two values
            (_mbid(17), 2),
        )
    )

    _decide("Chicago", mb, evidence)

    assert mb.calls == [f"lookup_artist:{_mbid(n)}" for n in (15, 14, 16)]


# --- No evidence -----------------------------------------------------------------------------


def test_an_untagged_artist_is_not_looked_up() -> None:
    mb = FakeMbClient()

    assert _decide("Marie Osmond", mb, LinkEvidence()) == LinkDecision(
        ArtistLinkOutcome.NO_EVIDENCE
    )
    assert mb.calls == []


def test_only_malformed_tags_are_no_evidence() -> None:
    mb = FakeMbClient()

    decision = _decide("Men Without Hats", mb, _tags("not-an-mbid", "{bad}"))

    assert decision == LinkDecision(ArtistLinkOutcome.NO_EVIDENCE)
    assert mb.calls == []


def test_an_empty_normalized_name_is_no_evidence() -> None:
    mb = FakeMbClient(artists={_mbid(18): _answer(18, "!!!")})

    assert decide_link("", _tags(_mbid(18)), mb) == LinkDecision(ArtistLinkOutcome.NO_EVIDENCE)
    assert mb.calls == []


# --- MusicBrainz's special-purpose artists ----------------------------------------------------


@pytest.mark.parametrize(("name", "mbid"), [("[unknown]", UNKNOWN), ("Various Artists", VARIOUS)])
def test_a_special_purpose_tag_stays_local_without_a_lookup(name: str, mbid: str) -> None:
    # Dev DB: local [unknown] has 7 files tagged with MusicBrainz's [unknown]; its name
    # normalizes to "unknown" on both sides, so without the list it would link.
    mb = FakeMbClient(artists={mbid: {"id": mbid, "name": name}})

    decision = _decide(name, mb, _tags(mbid))

    assert decision == LinkDecision(ArtistLinkOutcome.SPECIAL_PURPOSE)
    assert mb.calls == []


def test_an_unlisted_bracketed_musicbrainz_name_is_special_purpose() -> None:
    # The page lists no MBID for [christmas music]; the bracket rule catches it after a lookup.
    mb = FakeMbClient(artists={_mbid(19): _answer(19, "[christmas music]")})

    assert _decide("[christmas music]", mb, _tags(_mbid(19))) == LinkDecision(
        ArtistLinkOutcome.SPECIAL_PURPOSE
    )


def test_a_special_purpose_tag_beside_a_real_one_is_ignored() -> None:
    mb = FakeMbClient(artists={_mbid(20): _answer(20, "Nirvana")})

    decision = _decide("Nirvana", mb, _tags(f"{UNKNOWN}, {_mbid(20)}"))

    assert decision == _linked_to(20, "Nirvana")
    assert mb.calls == [f"lookup_artist:{_mbid(20)}"]


def test_the_special_purpose_list_is_the_twelve_documented_artists() -> None:
    # https://musicbrainz.org/doc/Style/Unknown_and_untitled/Special_purpose_artist, checked by
    # the controller on 2026-10-08. A typo would matter most for "MusicBrainz Test Artist",
    # whose name has no brackets for the fallback to catch.
    documented = frozenset(
        {
            VARIOUS,  # Various Artists
            UNKNOWN,  # [unknown]
            "eec63d3c-3b81-4ad4-b1e4-7c147d4d2b61",  # [no artist]
            "314e1c25-dde7-4e4d-b2f4-0a7b9f7c56dc",  # [dialogue]
            "f731ccc4-e22a-43af-a747-64213329e088",  # [anonymous]
            "9be7f096-97ec-4615-8957-8d40b5dcbc41",  # [traditional]
            "33cf029c-63b0-41a0-9855-be2a3665fb3b",  # [data]
            "a0ef7e1d-44ff-4039-9435-7d5fefdeecc9",  # [theatre]
            "80a8851f-444c-4539-892b-ad2a49292aa9",  # [language instruction]
            "66ea0139-149f-4a0c-8fbf-5ea9ec4a6e49",  # [Disney]
            "90068d37-bae7-4292-be4a-704c145bd616",  # [church chimes]
            "7e84f845-ac16-41fe-9ff8-df12eb32af55",  # MusicBrainz Test Artist
        }
    )

    assert documented == SPECIAL_PURPOSE_MBIDS


# --- Tag parsing and value objects -----------------------------------------------------------


def test_tag_values_are_split_parsed_lowercased_and_ranked_by_files() -> None:
    nirvana = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
    a, b = _mbid(21), _mbid(22)
    counts = ((f"{nirvana}, {a}", 2), (nirvana.upper(), 1), ("junk", 9), (f"{b};{a}", 1))

    assert tag_mbids(counts) == [a, nirvana, b]  # a: 3 files, nirvana: 3, b: 1; ties by MBID


def test_only_a_linked_decision_carries_a_candidate() -> None:
    candidate = ArtistLinkCandidate(mbid=_mbid(23), name="Yes", sort_name="Yes")

    with pytest.raises(InvalidLinkDecisionError):
        LinkDecision(LINKED)
    with pytest.raises(InvalidLinkDecisionError):
        LinkDecision(ArtistLinkOutcome.TAG_MISMATCH, candidate)


def test_a_candidate_needs_a_well_formed_mbid() -> None:
    with pytest.raises(InvalidMusicBrainzIdError):
        ArtistLinkCandidate(mbid="not-an-mbid", name="Yes", sort_name="Yes")


# --- link_artist -----------------------------------------------------------------------------


def test_link_artist_writes_the_link_in_place() -> None:
    repo = FakeArtistLinkingRepository()
    artist = repo.add(_local("NIRVANA"), _tags(_mbid(24)))
    mb = FakeMbClient(artists={_mbid(24): _answer(24, "Nirvana")})

    outcome = link_artist(artist, repo, mb)

    stored = repo.artists[artist.id]
    assert outcome == LINKED
    assert (stored.mbid, stored.origin, stored.name, stored.normalized_name) == (
        _mbid(24),
        CatalogSource.MUSICBRAINZ,
        "Nirvana",
        "nirvana",
    )
    assert stored.mb_lookup_outcome == LINKED
    assert repo.evidence_calls == ["nirvana"]
    assert repo.list_due() == []


def test_an_mbid_another_artist_holds_is_recorded_as_a_duplicate() -> None:
    repo = FakeArtistLinkingRepository()
    holder = repo.add(
        Artist(
            id=str(uuid4()),
            name="Nirvana (US)",
            sort_name="Nirvana",
            normalized_name="nirvana us",
            mbid=_mbid(25),
            origin=CatalogSource.MUSICBRAINZ,
            needs_enhancement=False,
        )
    )
    artist = repo.add(_local("Nirvana"), _tags(_mbid(25)))
    mb = FakeMbClient(artists={_mbid(25): _answer(25, "Nirvana")})

    outcome = link_artist(artist, repo, mb)

    assert outcome == ArtistLinkOutcome.DUPLICATE
    assert repo.artists[artist.id].mbid is None
    assert repo.artists[artist.id].mb_lookup_outcome == ArtistLinkOutcome.DUPLICATE
    assert repo.artists[holder.id].mb_lookup_outcome is None


BRACKETED = FakeMbClient(artists={_mbid(27): _answer(27, "[christmas music]")})


@pytest.mark.parametrize(
    ("evidence", "mb", "outcome"),
    [
        (LinkEvidence(), FakeMbClient(), ArtistLinkOutcome.NO_EVIDENCE),
        (_tags(UNKNOWN), FakeMbClient(), ArtistLinkOutcome.SPECIAL_PURPOSE),
        (_tags(_mbid(27)), BRACKETED, ArtistLinkOutcome.SPECIAL_PURPOSE),
        (_tags(_mbid(26)), FakeMbClient(), ArtistLinkOutcome.TAG_MISMATCH),
    ],
    ids=["no_evidence", "listed_placeholder", "bracketed_placeholder", "tag_mismatch"],
)
def test_a_decision_without_a_link_is_recorded_and_no_longer_due(
    evidence: LinkEvidence, mb: FakeMbClient, outcome: ArtistLinkOutcome
) -> None:
    repo = FakeArtistLinkingRepository()
    artist = repo.add(_local("Elvis Costello/Allen Toussaint"), evidence)

    assert link_artist(artist, repo, mb) == outcome
    assert repo.artists[artist.id].mb_lookup_outcome == outcome
    assert repo.artists[artist.id].mb_lookup_at is not None
    assert repo.list_due() == []
