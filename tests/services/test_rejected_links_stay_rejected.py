"""A rejected (name, artist) pair stays rejected (artist model phase 1b, SPEC S4; C19, AUD-R037).

The writer never makes a rejected pair active again. When the old artist matcher's automatic
match for a pending name targets an artist the name's link to is ``rejected``:

- its result is not written: no ``auto_matched`` status and no link (the pair stays rejected,
  with its origin and strength);
- the name ends ``needs_review`` with the new reason ``LINK_REJECTED`` (a ``ReasonCode``
  member; ``reason_code`` is TEXT, so no migration), written through the same guarded write
  as any result (AUD-R018): a name the user decided meanwhile keeps the user's decision;
- ``LINK_REJECTED`` starts no cascade: the name's songs stay as they were (pending).

A match to another artist is written as before, and the rejected pair stays rejected next to
it. A manual resolve to the rejected artist makes the pair active again as ``manual``: the
user's decision wins (Lance's review rule: a change may widen review, never decide or hide
what the user decided).
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
from backend.domain.enums import (
    LogNameLinkOrigin,
    LogNameLinkState,
    MatchStatus,
    MatchTier,
    ReasonCode,
)
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.repositories.log_name_links import LogNameLinkRepository
from backend.services.artist_matching_service import (
    ArtistMatchingRepos,
    match_artists_for_playlist,
)
from backend.services.artist_resolution_service import match_by_hand
from backend.services.log_name_link_service import LogNameLinkService
from backend.services.normalization import compute_normalized_signature, normalize_artist
from backend.services.repository_factory import RepositoryFactory, artist_resolution_repos
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.log_name_links import FakeLogNameLinkRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient
from tests.matching_seed import seed_link

METALLICA = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
MEGADETH = "a9044915-8be3-4c7e-b11f-9e2d2ea0a91e"
OZZY = "8aa5b65a-5b3c-4029-92bf-47a544356934"
# Cut off at 30 characters (the MusicBrainz tier) and far from "Ozzy Osbourne" (33), so a
# catalog artist of that name is no local candidate (the floor is 50; 1a's S6 test found this).
TRUNCATED = "BLIZZARD OF OZZ TOUR LIVE 1981"


def test_link_rejected_is_a_reason_code() -> None:
    assert ReasonCode.LINK_REJECTED.value == "LINK_REJECTED"


@dataclass
class _World:
    names: BroadcastArtistRepository
    songs: BroadcastTrackIdentityRepository
    catalog: ArtistCatalogRepository
    links: LogNameLinkRepository
    playlist_id: UUID
    pending: Callable[[str], tuple[BroadcastArtist, UUID]]
    """A pending name played on the playlist, with one pending song; the name and the song."""

    def run(self, mb_client: FakeMbClient | None = None) -> None:
        match_artists_for_playlist(
            playlist_id=self.playlist_id,
            repos=ArtistMatchingRepos(
                broadcast_artist_repo=self.names,
                track_identity_repo=self.songs,
                artist_repo=self.catalog,
                match_repo=FakeMatchRepository(),
                rules_repo=FakeMappingRuleRepository(),
                link_repo=self.links,
            ),
            mb_client=mb_client or FakeMbClient(),
        )

    def name_state(self, name_id: UUID) -> tuple[MatchStatus, ReasonCode | None]:
        name = self.names.get_by_id(name_id)
        assert name is not None
        return name.match_status, name.reason_code

    def song_status(self, song_id: UUID) -> MatchStatus:
        song = self.songs.get_by_id(song_id)
        assert song is not None
        return song.match_status

    def link_states(self, name_id: UUID) -> dict[str, tuple[str, str, int]]:
        return {
            link.artist_id: (link.state.value, link.origin.value, link.strength)
            for link in self.links.list_by_name(name_id)
        }

    def reject(self, name_id: UUID, target: str, strength: int = 100) -> str:
        """The curator's rejected link to the artist *target* names; the artist's id."""
        link = seed_link(
            self.links,
            self.catalog,
            name_id,
            target,
            origin=LogNameLinkOrigin.MANUAL,
            state=LogNameLinkState.REJECTED,
            strength=strength,
        )
        return link.artist_id


def _song(name: BroadcastArtist) -> BroadcastTrackIdentity:
    return BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=name.id,
        original_title="Song",
        normalized_title="song",
        normalized_signature=compute_normalized_signature(name.normalized_name, "song"),
    )


def _fake_world(names: FakeBroadcastArtistRepository | None = None) -> _World:
    names = names or FakeBroadcastArtistRepository()
    songs = FakeBroadcastTrackIdentityRepository()
    catalog = FakeArtistRepository()
    playlist_id = uuid4()

    def pending(spelling: str) -> tuple[BroadcastArtist, UUID]:
        name = names.upsert(
            BroadcastArtist(
                id=uuid4(), original_name=spelling, normalized_name=normalize_artist(spelling)
            )
        )
        names.register_playlist_artist(playlist_id, name.id)
        song = songs.upsert(_song(name))
        songs.register_playlist_identity(playlist_id, song.id)
        return name, song.id

    return _World(
        names=names,
        songs=songs,
        catalog=catalog,
        links=FakeLogNameLinkRepository(artists=catalog, names=names),
        playlist_id=playlist_id,
        pending=pending,
    )


@pytest.fixture
def pg_conn(migrated_db: str) -> Iterator[psycopg.Connection[Any]]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield conn


def _pg_world(conn: psycopg.Connection[Any]) -> _World:
    factory = RepositoryFactory(conn)
    station = factory.broadcast_stations.create(BroadcastStation(id=uuid4(), call_letters="KRJL"))
    playlist = factory.broadcast_playlists.create(
        BroadcastPlaylist(
            id=uuid4(), name="show.csv", content_hash=uuid4().hex, station_id=station.id
        )
    )

    def pending(spelling: str) -> tuple[BroadcastArtist, UUID]:
        name = factory.broadcast_artists.upsert(
            BroadcastArtist(
                id=uuid4(), original_name=spelling, normalized_name=normalize_artist(spelling)
            )
        )
        song = factory.broadcast_identities.upsert(_song(name))
        factory.broadcast_events.create(
            BroadcastPlayEvent(
                id=uuid4(),
                identity_id=song.id,
                playlist_id=playlist.id,
                played_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        )
        return name, song.id

    return _World(
        names=factory.broadcast_artists,
        songs=factory.broadcast_identities,
        catalog=factory.artists,
        links=PgLogNameLinkRepository(conn),
        playlist_id=playlist.id,
        pending=pending,
    )


def _musicbrainz_artist(catalog: ArtistCatalogRepository, mbid: str, name: str) -> str:
    """A catalog artist holding *mbid*, made as the matcher's MusicBrainz upsert makes one (the
    PostgreSQL ``upsert`` stores no MBID); its id."""
    return catalog.upsert_musicbrainz_artist(
        mbid=mbid, name=name, sort_name=name, normalized_name=normalize_artist(name)
    )


def _metallica(catalog: ArtistCatalogRepository) -> str:
    return _musicbrainz_artist(catalog, METALLICA, "Metallica")


def _assert_left_in_review(world: _World, name: BroadcastArtist, song_id: UUID, held: str) -> None:
    assert world.name_state(name.id) == (MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_REJECTED)
    # The pair stays rejected, as the curator left it, and no other link is written.
    assert world.link_states(name.id) == {held: ("rejected", "manual", 100)}
    # No cascade: the song is still pending.
    assert world.song_status(song_id) == MatchStatus.PENDING


# --- the matcher ---------------------------------------------------------------------------------


def test_the_matcher_leaves_a_rejected_pair_rejected() -> None:
    world = _fake_world()
    metallica = _metallica(world.catalog)
    name, song = world.pending("METALLICA")
    world.reject(name.id, METALLICA)

    world.run()

    _assert_left_in_review(world, name, song, metallica)


@pytest.mark.integration
def test_the_matcher_leaves_a_rejected_pair_rejected_on_postgres(
    pg_conn: psycopg.Connection[Any],
) -> None:
    world = _pg_world(pg_conn)
    metallica = _metallica(world.catalog)
    name, song = world.pending("METALLICA")
    world.reject(name.id, METALLICA)

    world.run()
    pg_conn.commit()

    _assert_left_in_review(world, name, song, metallica)


def test_a_rejected_musicbrainz_target_is_not_linked() -> None:
    """The MusicBrainz tier finds the artist the curator rejected for this (cut-off) name."""
    world = _fake_world()
    ozzy = _musicbrainz_artist(world.catalog, OZZY, "Ozzy Osbourne")
    name, song = world.pending(TRUNCATED)
    world.reject(name.id, OZZY)

    world.run(
        FakeMbClient(
            responses={
                TRUNCATED: [
                    {
                        "id": OZZY,
                        "name": "Ozzy Osbourne",
                        "sort-name": "Osbourne, Ozzy",
                        "score": 100,
                    }
                ]
            }
        )
    )

    _assert_left_in_review(world, name, song, ozzy)


def test_another_artist_is_still_linked() -> None:
    """Only the rejected pair is held back: a match to another artist is written as before,
    and the rejected pair stays rejected next to it."""
    world = _fake_world()
    metallica = _metallica(world.catalog)
    name, _ = world.pending("METALLICA")
    megadeth = world.reject(name.id, MEGADETH, strength=70)

    world.run()

    assert world.name_state(name.id) == (MatchStatus.AUTO_MATCHED, None)
    links = world.link_states(name.id)
    assert links[megadeth] == ("rejected", "manual", 70)
    assert links[metallica][:2] == ("active", "automatic")


def test_other_results_are_written_as_before() -> None:
    """Only an automatic match to the rejected artist is held back: a name holding a rejected
    link that the matcher defers (no local or MusicBrainz match) ends DEFERRED_RETRY, and its
    songs are deferred as before (AUD-R028, AUD-R038), not LINK_REJECTED."""
    world = _fake_world()
    metallica = _metallica(world.catalog)
    name, song = world.pending("ZZZYX QWERTY")
    world.reject(name.id, METALLICA)

    world.run()

    assert world.name_state(name.id) == (MatchStatus.NEEDS_REVIEW, ReasonCode.DEFERRED_RETRY)
    deferred = world.songs.get_by_id(song)
    assert deferred is not None
    assert (deferred.match_status, deferred.reason_code) == (
        MatchStatus.NEEDS_REVIEW,
        ReasonCode.DEFERRED_RETRY,
    )
    assert world.link_states(name.id) == {metallica: ("rejected", "manual", 100)}


def test_a_name_the_user_decided_meanwhile_keeps_the_decision() -> None:
    """AUD-R018: the LINK_REJECTED write is guarded on the pending status the run read, like
    every result; the user's decision made while the run was matching wins."""

    class DecidedWhileMatching(FakeBroadcastArtistRepository):
        def get_pending_for_playlist(self, playlist_id: UUID) -> list[BroadcastArtist]:
            read = super().get_pending_for_playlist(playlist_id)
            for name in read:
                self.update_match_status(name.id, MatchStatus.MANUAL_REJECTED)
            return read

    world = _fake_world(DecidedWhileMatching())
    metallica = _metallica(world.catalog)
    name, song = world.pending("METALLICA")
    world.reject(name.id, METALLICA)

    world.run()

    assert world.name_state(name.id) == (MatchStatus.MANUAL_REJECTED, None)
    assert world.link_states(name.id) == {metallica: ("rejected", "manual", 100)}
    assert world.song_status(song) == MatchStatus.PENDING


def test_the_link_writer_never_reactivates_a_rejected_pair() -> None:
    """The writer itself (1a's rule, which S4 relies on): an automatic link to a rejected pair
    writes nothing."""
    world = _fake_world()
    metallica = _metallica(world.catalog)
    name, _ = world.pending("METALLICA")
    world.reject(name.id, METALLICA)

    LogNameLinkService(world.links, world.catalog).link_automatically(
        name.id, METALLICA, MatchTier.NORMALIZATION, 100.0
    )

    assert world.link_states(name.id) == {metallica: ("rejected", "manual", 100)}


# --- the user's decision wins ---------------------------------------------------------------------


def test_a_manual_resolve_makes_the_pair_active_again() -> None:
    world = _fake_world()
    metallica = _metallica(world.catalog)
    name, _ = world.pending("METALLICA")
    world.reject(name.id, METALLICA, strength=40)

    LogNameLinkService(world.links, world.catalog).link_by_hand(name.id, METALLICA)

    assert world.link_states(name.id) == {metallica: ("active", "manual", 100)}


@pytest.mark.integration
def test_a_manual_resolve_makes_the_pair_active_again_on_postgres(
    pg_conn: psycopg.Connection[Any],
) -> None:
    """Through the Resolve service, as the route calls it, after the matcher left the name in
    review."""
    world = _pg_world(pg_conn)
    metallica = _metallica(world.catalog)
    name, _ = world.pending("METALLICA")
    world.reject(name.id, METALLICA)
    world.run()

    match_by_hand(name.id, METALLICA, artist_resolution_repos(RepositoryFactory(pg_conn)))
    pg_conn.commit()

    assert world.name_state(name.id)[0] == MatchStatus.MANUAL_MATCHED
    assert world.link_states(name.id) == {metallica: ("active", "manual", 100)}
