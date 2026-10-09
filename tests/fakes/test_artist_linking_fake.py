"""The linker's fake mirrors the Pg adapter's work list and write guards (AUD-R026, D15)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from backend.domain.catalog import Artist, ArtistLinkCandidate, InvalidLinkDecisionError
from backend.domain.enums import ArtistLinkOutcome, CatalogSource
from backend.repositories.artist_linking import ArtistLinkingRepository
from backend.services.normalization import normalize_artist
from tests.fakes.artist_linking import FakeArtistLinkingRepository

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
NIRVANA = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"


def _artist(
    name: str,
    *,
    mbid: str | None = None,
    origin: CatalogSource = CatalogSource.LOCAL,
    looked_up_at: datetime | None = None,
    outcome: ArtistLinkOutcome = ArtistLinkOutcome.TAG_MISMATCH,
) -> Artist:
    return Artist(
        id=str(uuid4()),
        name=name,
        sort_name=name,
        normalized_name=normalize_artist(name),
        mbid=mbid,
        origin=origin,
        needs_enhancement=False,
        mb_lookup_at=looked_up_at,
        mb_lookup_outcome=outcome if looked_up_at else None,
    )


def test_the_fake_implements_the_port() -> None:
    assert issubclass(FakeArtistLinkingRepository, ArtistLinkingRepository)
    FakeArtistLinkingRepository()  # TypeError here if an abstract method is missing


def test_due_artists_are_local_unlinked_and_not_looked_up_since_their_last_file() -> None:
    repo = FakeArtistLinkingRepository()
    never = repo.add(_artist("Yes"))
    repo.add(_artist("Boston", looked_up_at=T0))
    refreshed = repo.add(_artist("Chicago", looked_up_at=T0), file_indexed_at=T0 + HOUR)
    repo.add(_artist("America", looked_up_at=T0), file_indexed_at=T0 - HOUR)
    repo.add(_artist("Nirvana", mbid=NIRVANA, origin=CatalogSource.MUSICBRAINZ))

    assert [a.id for a in repo.list_due()] == [refreshed.id, never.id]  # by normalized name


def test_a_link_is_refused_when_another_artist_holds_the_mbid() -> None:
    repo = FakeArtistLinkingRepository()
    repo.add(_artist("Nirvana (US)", mbid=NIRVANA, origin=CatalogSource.MUSICBRAINZ))
    local = repo.add(_artist("Nirvana"))

    written = repo.link(local.id, ArtistLinkCandidate(NIRVANA, "Nirvana", "Nirvana"))

    assert written is False
    assert local.mbid is None
    assert local.mb_lookup_outcome is None


def test_a_link_writes_the_mbid_in_place() -> None:
    repo = FakeArtistLinkingRepository()
    local = repo.add(_artist("NIRVANA"))

    written = repo.link(local.id, ArtistLinkCandidate(NIRVANA, "Nirvana", "Nirvana", "grunge"))

    assert written is True
    assert (local.mbid, local.origin, local.name, local.disambiguation) == (
        NIRVANA,
        CatalogSource.MUSICBRAINZ,
        "Nirvana",
        "grunge",
    )
    assert local.mb_lookup_outcome == ArtistLinkOutcome.LINKED
    assert local.linked_by_lookup is True
    assert repo.list_due() == []


def test_record_outcome_refuses_linked() -> None:
    repo = FakeArtistLinkingRepository()
    local = repo.add(_artist("Boston"))

    with pytest.raises(InvalidLinkDecisionError):
        repo.record_outcome(local.id, ArtistLinkOutcome.LINKED)


def test_names_linked_since_hold_the_catalog_and_broadcast_names_of_later_links() -> None:
    repo = FakeArtistLinkingRepository()
    repo.add(
        _artist(
            "Nirvana",
            mbid=NIRVANA,
            origin=CatalogSource.MUSICBRAINZ,
            looked_up_at=T0 + HOUR,
            outcome=ArtistLinkOutcome.LINKED,
        ),
        broadcast_names=("nirvana uk",),
    )
    repo.add(_artist("Boston", looked_up_at=T0 + HOUR))  # not linked
    repo.add(
        _artist(
            "Yes",
            mbid="00000000-0000-4000-8000-000000000001",
            origin=CatalogSource.MUSICBRAINZ,
            looked_up_at=T0 - HOUR,
            outcome=ArtistLinkOutcome.LINKED,
        )
    )

    assert repo.normalized_names_linked_since(T0) == {"nirvana", "nirvana uk"}
