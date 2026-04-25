from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.domain.broadcast import BroadcastArtist
from backend.domain.catalog import Artist
from backend.domain.enums import MatchStatus, TargetType
from backend.domain.matching import MappingRule
from backend.services.artist_matching_service import match_artists_for_playlist
from backend.services.normalization import normalize_artist
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository


class StubMbClient:
    """Returns canned results for testing."""
    def __init__(self, results: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._results = results or {}
        # MusicBrainzClientProtocol now declares counter attributes; keep them
        # at 0 — these tests don't exercise summary-event behavior.
        self.live_fetches: int = 0
        self.cache_hits: int = 0

    def search_artist(self, name: str) -> list[dict[str, Any]]:
        return self._results.get(name, [])


def _make_pending_artist(
    name: str,
    broadcast_artist_repo: FakeBroadcastArtistRepository,
    playlist_id: Any,
) -> BroadcastArtist:
    artist = BroadcastArtist(
        id=uuid4(), original_name=name,
        normalized_name=normalize_artist(name),
    )
    broadcast_artist_repo.upsert(artist)
    broadcast_artist_repo.register_playlist_artist(playlist_id, artist.id)
    return artist


def test_tier1_exact_match() -> None:
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    # Seed canonical artist — mbid must be populated so
    # NormalizationStrategy will emit an MBID as Match.target_id (consumed
    # downstream by the identity-tier MBID-graph lookup).
    artist_repo.upsert(Artist(
        id="mbid-metallica", name="Metallica", sort_name="Metallica",
        mbid="mbid-metallica",
    ))

    _make_pending_artist("METALLICA", broadcast_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=FakeMappingRuleRepository(),
        mb_client=StubMbClient(),
    )

    artists = list(broadcast_artist_repo._data.values())
    assert artists[0].match_status == MatchStatus.AUTO_MATCHED


def test_tier3_mb_api_auto_matched() -> None:
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    _make_pending_artist("OZZY OSBOURNE", broadcast_artist_repo, playlist_id)

    mb_client = StubMbClient({
        "OZZY OSBOURNE": [
            {"id": "mbid-ozzy", "name": "Ozzy Osbourne",
             "sort-name": "Osbourne, Ozzy", "score": 100},
        ]
    })

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=FakeMappingRuleRepository(),
        mb_client=mb_client,
    )

    artists = list(broadcast_artist_repo._data.values())
    assert artists[0].match_status == MatchStatus.AUTO_MATCHED
    assert artist_repo.get_by_id("mbid-ozzy") is not None


def test_no_match_any_tier_needs_review() -> None:
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()

    _make_pending_artist("UNKNOWN BAND XYZ", broadcast_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=FakeArtistRepository(),
        match_repo=FakeMatchRepository(),
        rules_repo=FakeMappingRuleRepository(),
        mb_client=StubMbClient(),
    )

    artists = list(broadcast_artist_repo._data.values())
    assert artists[0].match_status == MatchStatus.NEEDS_REVIEW


def test_global_rule_exact_match() -> None:
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    rules_repo = FakeMappingRuleRepository()
    match_repo = FakeMatchRepository()

    rules_repo.create(MappingRule(
        id=uuid4(), source_pattern="ac dc",
        target_type=TargetType.ARTIST, target_id="mbid-acdc", priority=10,
    ))

    _make_pending_artist("AC/DC", broadcast_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=FakeArtistRepository(),
        match_repo=match_repo,
        rules_repo=rules_repo,
        mb_client=StubMbClient(),
    )

    artists = list(broadcast_artist_repo._data.values())
    assert artists[0].match_status == MatchStatus.AUTO_MATCHED


def test_cascade_auto_rejected() -> None:
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    track_identity_repo = FakeBroadcastTrackIdentityRepository()

    artist = _make_pending_artist("BAD ARTIST", broadcast_artist_repo, playlist_id)
    # Manually set to AUTO_REJECTED to test cascade
    broadcast_artist_repo.update_match_status(artist.id, MatchStatus.AUTO_REJECTED)

    from backend.domain.broadcast import BroadcastTrackIdentity
    identity = BroadcastTrackIdentity(
        id=uuid4(), broadcast_artist_id=artist.id,
        original_title="Song", normalized_title="song",
        normalized_signature="cascade_test_sig_00000000000000",
    )
    track_identity_repo.upsert(identity)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=track_identity_repo,
        artist_repo=FakeArtistRepository(),
        match_repo=FakeMatchRepository(),
        rules_repo=FakeMappingRuleRepository(),
        mb_client=StubMbClient(),
    )

    identities = list(track_identity_repo._data.values())
    assert identities[0].match_status == MatchStatus.AUTO_REJECTED
