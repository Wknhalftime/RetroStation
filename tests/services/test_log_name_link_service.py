"""LogNameLinkService's rules beyond the locked 1a tests (artist model phase 1a; the diff review
of PR #214): an automatic write keeps a copied link's v0 legacy evidence, which phase 1c's
demotion reads; it never overwrites a manual or a rejected pair (C18, C19); and a target an
artist already holds is never given a second stub."""

from __future__ import annotations

from uuid import UUID, uuid4

from backend.domain.broadcast import BroadcastArtist
from backend.domain.catalog import Artist
from backend.domain.enums import LogNameLinkOrigin, LogNameLinkState, MatchTier
from backend.domain.matching import LogNameLink
from backend.services.log_name_link_service import LogNameLinkService
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.log_name_links import FakeLogNameLinkRepository

LEGACY = {"match_id": "m-1", "target_id": "mbid-a", "tier": "normalization", "score": 72.0}


def _world() -> tuple[LogNameLinkService, FakeLogNameLinkRepository, UUID, Artist]:
    catalog, names = FakeArtistRepository(), FakeBroadcastArtistRepository()
    links = FakeLogNameLinkRepository(artists=catalog, names=names)
    name = names.upsert(BroadcastArtist(id=uuid4(), original_name="A", normalized_name="a"))
    artist = catalog.upsert(Artist(id="artist-a", name="A Band", sort_name="A Band", mbid="mbid-a"))
    return LogNameLinkService(links, catalog), links, name.id, artist


def _held(
    links: FakeLogNameLinkRepository,
    name_id: UUID,
    artist: Artist,
    origin: LogNameLinkOrigin,
    state: LogNameLinkState = LogNameLinkState.ACTIVE,
) -> None:
    links.upsert_link(
        LogNameLink(
            broadcast_artist_id=name_id,
            artist_id=artist.id,
            mbid=artist.mbid,
            state=state,
            origin=origin,
            strength=72,
            evidence={"version": 0, "legacy": LEGACY},
        )
    )


def test_an_automatic_write_keeps_the_legacy_evidence() -> None:
    service, links, name_id, artist = _world()
    _held(links, name_id, artist, LogNameLinkOrigin.LEGACY)

    service.link_automatically(name_id, "mbid-a", MatchTier.NORMALIZATION, 91.0)

    [link] = links.list_by_name(name_id)
    assert (link.origin, link.strength) == (LogNameLinkOrigin.AUTOMATIC, 91)
    assert dict(link.evidence) == {
        "version": 0,
        "tier": "normalization",
        "score": 91.0,
        "legacy": LEGACY,
    }


def test_a_manual_or_rejected_pair_is_left_alone() -> None:
    for origin, state in (
        (LogNameLinkOrigin.MANUAL, LogNameLinkState.ACTIVE),
        (LogNameLinkOrigin.LEGACY, LogNameLinkState.REJECTED),
    ):
        service, links, name_id, artist = _world()
        _held(links, name_id, artist, origin, state)

        service.link_automatically(name_id, "mbid-a", MatchTier.NORMALIZATION, 91.0)

        [link] = links.list_by_name(name_id)
        assert (link.origin, link.state, link.strength) == (origin, state, 72)


def test_a_held_mbid_gets_no_second_stub() -> None:
    catalog = FakeArtistRepository()
    first = catalog.create_stub("mbid-z")

    again = catalog.create_stub("mbid-z")

    assert again.id == first.id
    assert [a.mbid for a in catalog._data.values()] == ["mbid-z"]
