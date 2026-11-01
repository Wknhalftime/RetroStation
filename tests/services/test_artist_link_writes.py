"""The artist matcher writes its link to ``log_name_links`` (artist model phase 1a, SPEC S6;
C30, AUD-R035, AUD-R018).

On an automatic match, ``_persist_artist_result``:

- upserts the catalog artist first, when the result carries a MusicBrainz candidate;
- then links the artist whose ``mbid`` equals the result's target (a catalog id, which the
  exact pass gives a local-only artist, links that artist). When the upsert returned another
  MBID's artist, because that one holds the name, or there is no catalog row at all (a mapping
  rule's target), a stub of S3's shape is made for the target and linked;
- writes the link through the link service, never to ``matches``: origin ``automatic``,
  state ``active``, strength the score, evidence v0 with the tier and the score.

A result that is not an automatic match writes no link. The AUD-R018 guard is unchanged: when
it fails nothing is written (the locked ``test_guarded_worker_writes.py``).

The links fake checks its foreign key against the catalog fake, so a link written before its
artist exists fails, as on PostgreSQL. The name clash also runs on PostgreSQL, whose catalog
upsert is the one that returns another MBID's artist.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.log_name_links import PgLogNameLinkRepository
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.catalog import Artist
from backend.domain.enums import (
    CatalogSource,
    CreditRole,
    LogNameLinkOrigin,
    LogNameLinkState,
    MatchStatus,
    TargetType,
)
from backend.domain.matching import LogNameLink, MappingRule
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.log_name_links import LogNameLinkRepository
from backend.repositories.matches import MatchRepository
from backend.services.artist_matching_service import (
    ArtistMatchingRepos,
    match_artists_for_playlist,
)
from backend.services.normalization import compute_normalized_signature, normalize_artist
from backend.services.repository_factory import RepositoryFactory
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.log_name_links import FakeLogNameLinkRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient

METALLICA = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
OZZY = "8aa5b65a-5b3c-4029-92bf-47a544356934"
OTHER_OZZY = "00000000-0000-4000-8000-00000000007a"
TRUNCATED = "OZZY OSBOURNE THE METAL LEGEND"  # 30 characters: the MusicBrainz tier
# Also cut off, and far enough from "Ozzy Osbourne" (33) that a catalog artist of that name is
# no local candidate (the floor is 50), so the name reaches the MusicBrainz tier.
TRUNCATED_ELSEWHERE = "BLIZZARD OF OZZ TOUR LIVE 1981"


def _ozzy_found(name: str = TRUNCATED) -> FakeMbClient:
    return FakeMbClient(
        responses={
            name: [
                {"id": OZZY, "name": "Ozzy Osbourne", "sort-name": "Osbourne, Ozzy", "score": 100}
            ]
        }
    )


@dataclass
class _World:
    names: BroadcastArtistRepository
    catalog: ArtistCatalogRepository
    links: LogNameLinkRepository
    matches: MatchRepository
    rules: FakeMappingRuleRepository
    playlist_id: UUID
    pending: Callable[[str], BroadcastArtist]

    def run(self, mb_client: FakeMbClient | None = None) -> None:
        match_artists_for_playlist(
            playlist_id=self.playlist_id,
            repos=ArtistMatchingRepos(
                broadcast_artist_repo=self.names,
                track_identity_repo=FakeBroadcastTrackIdentityRepository(),
                artist_repo=self.catalog,
                match_repo=self.matches,
                rules_repo=self.rules,
                link_repo=self.links,
            ),
            mb_client=mb_client or FakeMbClient(),
        )

    def only_link(self, name: BroadcastArtist) -> LogNameLink:
        [link] = self.links.list_by_name(name.id)
        return link


def _fake_world() -> _World:
    names = FakeBroadcastArtistRepository()
    catalog = FakeArtistRepository()
    playlist_id = uuid4()

    def pending(name: str) -> BroadcastArtist:
        artist = names.upsert(
            BroadcastArtist(id=uuid4(), original_name=name, normalized_name=normalize_artist(name))
        )
        names.register_playlist_artist(playlist_id, artist.id)
        return artist

    return _World(
        names=names,
        catalog=catalog,
        links=FakeLogNameLinkRepository(artists=catalog, names=names),
        matches=FakeMatchRepository(),
        rules=FakeMappingRuleRepository(),
        playlist_id=playlist_id,
        pending=pending,
    )


@pytest.fixture
def pg_conn(migrated_db: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield conn


def _pg_world(conn: psycopg.Connection[Any]) -> _World:
    """The same world on PostgreSQL: a playlist whose plays make each seeded name pending."""
    factory = RepositoryFactory(conn)
    station = factory.broadcast_stations.create(BroadcastStation(id=uuid4(), call_letters="KRCK"))
    playlist = factory.broadcast_playlists.create(
        BroadcastPlaylist(
            id=uuid4(), name="show.csv", content_hash=uuid4().hex, station_id=station.id
        )
    )

    def pending(name: str) -> BroadcastArtist:
        artist = factory.broadcast_artists.upsert(
            BroadcastArtist(id=uuid4(), original_name=name, normalized_name=normalize_artist(name))
        )
        song = factory.broadcast_identities.upsert(
            BroadcastTrackIdentity(
                id=uuid4(),
                broadcast_artist_id=artist.id,
                original_title="Song",
                normalized_title="song",
                normalized_signature=compute_normalized_signature(artist.normalized_name, "song"),
            )
        )
        factory.broadcast_events.create(
            BroadcastPlayEvent(
                id=uuid4(),
                identity_id=song.id,
                playlist_id=playlist.id,
                played_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        )
        return artist

    return _World(
        names=factory.broadcast_artists,
        catalog=factory.artists,
        links=PgLogNameLinkRepository(conn),
        matches=factory.matches,
        rules=FakeMappingRuleRepository(),
        playlist_id=playlist.id,
        pending=pending,
    )


def test_auto_match_writes_a_link() -> None:
    world = _fake_world()
    metallica = world.catalog.upsert(
        Artist(id="artist-metallica", name="Metallica", sort_name="Metallica", mbid=METALLICA)
    )
    local = world.catalog.upsert_local_artist("Van Halen", normalize_artist("Van Halen"))
    by_mbid = world.pending("METALLICA")
    by_local_id = world.pending("VAN HALEN")

    world.run()

    linked = world.only_link(by_mbid)
    assert (linked.artist_id, linked.mbid) == (metallica.id, METALLICA)
    assert (linked.state, linked.origin, linked.strength) == (
        LogNameLinkState.ACTIVE,
        LogNameLinkOrigin.AUTOMATIC,
        100,
    )
    assert dict(linked.evidence) == {"version": 0, "tier": "normalization", "score": 100.0}
    assert (linked.credit_part, linked.role, linked.join_after) == (0, CreditRole.MAIN, None)
    local_link = world.only_link(by_local_id)
    assert (local_link.artist_id, local_link.mbid) == (local, None)
    for name in (by_mbid, by_local_id):
        stored = world.names.get_by_id(name.id)
        assert stored is not None and stored.match_status == MatchStatus.AUTO_MATCHED


def test_catalog_artist_first() -> None:
    """A MusicBrainz hit with no catalog row: the upsert makes the artist, and the link points
    at it, named, not at a stub made for a link written first."""
    world = _fake_world()
    name = world.pending(TRUNCATED)

    world.run(_ozzy_found())

    link = world.only_link(name)
    artist = world.catalog.get_by_id(link.artist_id)
    assert artist is not None
    assert (artist.mbid, artist.name, artist.sort_name) == (OZZY, "Ozzy Osbourne", "Osbourne, Ozzy")
    assert [a.mbid for a in world.catalog.list_all()] == [OZZY]
    assert (link.mbid, link.origin, link.strength) == (OZZY, LogNameLinkOrigin.AUTOMATIC, 100)
    assert dict(link.evidence) == {"version": 0, "tier": "musicbrainz_api", "score": 100.0}


def _assert_the_target_got_a_stub(world: _World, name: BroadcastArtist, held_id: str) -> None:
    link = world.only_link(name)
    held = world.catalog.get_by_id(held_id)
    stub = world.catalog.get_by_id(link.artist_id)
    assert held is not None and stub is not None
    assert (held.mbid, held.name) == (OTHER_OZZY, "Ozzy Osbourne")  # left as it was
    assert link.artist_id != held_id
    assert stub.id != OZZY  # a stub has its own id
    assert (stub.mbid, stub.name, stub.sort_name, stub.normalized_name) == (OZZY, OZZY, OZZY, None)
    assert (stub.origin, stub.needs_enhancement) == (CatalogSource.MUSICBRAINZ, True)
    assert link.mbid == OZZY


def test_name_clash_links_the_target() -> None:
    """The name is held by another MBID's artist, so the upsert returns that artist; the link
    goes to the artist of the result's own MBID, a stub (db/repositories/artists.py)."""
    world = _fake_world()
    held = world.catalog.upsert_musicbrainz_artist(
        mbid=OTHER_OZZY,
        name="Ozzy Osbourne",
        sort_name="Osbourne, Ozzy",
        normalized_name=normalize_artist("Ozzy Osbourne"),
    )
    name = world.pending(TRUNCATED_ELSEWHERE)

    world.run(_ozzy_found(TRUNCATED_ELSEWHERE))

    _assert_the_target_got_a_stub(world, name, held)


@pytest.mark.integration
def test_name_clash_links_the_target_on_postgres(pg_conn: psycopg.Connection[Any]) -> None:
    world = _pg_world(pg_conn)
    held = world.catalog.upsert_musicbrainz_artist(
        mbid=OTHER_OZZY,
        name="Ozzy Osbourne",
        sort_name="Osbourne, Ozzy",
        normalized_name=normalize_artist("Ozzy Osbourne"),
    )
    name = world.pending(TRUNCATED_ELSEWHERE)

    world.run(_ozzy_found(TRUNCATED_ELSEWHERE))

    _assert_the_target_got_a_stub(world, name, held)
    rows = pg_conn.execute("SELECT count(*) AS n FROM matches WHERE identity_id IS NULL").fetchone()
    assert rows is not None and rows["n"] == 0


def test_unknown_target_gets_a_stub() -> None:
    """A mapping rule's target with no catalog row (the locked .ambr pins ``mbid-ruleband``):
    one stub per MBID, shared by every name that reaches it."""
    world = _fake_world()
    first, second = world.pending("RULE BAND"), world.pending("BAND OF RULES")
    assert first.id != second.id
    for name in (first, second):
        world.rules.create(
            MappingRule(
                id=uuid4(),
                source_pattern=name.normalized_name,
                target_type=TargetType.ARTIST,
                target_id="mbid-ruleband",
                priority=10,
            )
        )

    world.run()

    links = [world.only_link(first), world.only_link(second)]
    assert links[0].artist_id == links[1].artist_id
    stub = world.catalog.get_by_id(links[0].artist_id)
    assert stub is not None
    assert (stub.mbid, stub.name, stub.sort_name, stub.normalized_name) == (
        "mbid-ruleband",
        "mbid-ruleband",
        "mbid-ruleband",
        None,
    )
    assert (stub.origin, stub.needs_enhancement) == (CatalogSource.MUSICBRAINZ, True)
    assert dict(links[0].evidence) == {"version": 0, "tier": "manual", "score": 100.0}
    assert world.catalog.list_all() == []  # an unnamed stub is not a matching candidate (S15)


def test_a_result_that_is_not_an_automatic_match_writes_no_active_link() -> None:
    """No candidate at all, or a lone fuzzy one (69, LOW_CONFIDENCE, with a target): the name
    goes to review with no active link (from 1c such a result leaves a suggestion)."""
    world = _fake_world()
    world.catalog.upsert(
        Artist(id="artist-metallica", name="Metallica", sort_name="Metallica", mbid=METALLICA)
    )
    nothing = world.pending("UNKNOWN BAND XYZ")
    weak = world.pending("METALLICA TRIBUTE")

    world.run()

    for name in (nothing, weak):
        stored = world.names.get_by_id(name.id)
        assert stored is not None and stored.match_status != MatchStatus.AUTO_MATCHED
        active = [
            link
            for link in world.links.list_by_name(name.id)
            if link.state == LogNameLinkState.ACTIVE
        ]
        assert active == []
