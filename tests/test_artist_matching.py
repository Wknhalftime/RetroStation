from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.domain.enums import MatchStatus, TargetType
from backend.domain.models import Artist, GlobalMappingRule, LogArtist
from backend.services.artist_matching_service import match_artists_for_playlist
from backend.services.normalization import normalize_artist
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.global_mapping_rules import FakeGlobalMappingRuleRepository
from tests.fakes.log_artists import FakeLogArtistRepository
from tests.fakes.log_identities import FakeLogIdentityRepository
from tests.fakes.matches import FakeMatchRepository


class StubMbClient:
    """Returns canned results for testing."""
    def __init__(self, results: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._results = results or {}

    def search_artist(self, name: str) -> list[dict[str, Any]]:
        return self._results.get(name, [])


def _make_pending_artist(
    name: str,
    log_artist_repo: FakeLogArtistRepository,
    playlist_id: Any,
) -> LogArtist:
    artist = LogArtist(
        id=uuid4(), original_name=name,
        normalized_name=normalize_artist(name),
    )
    log_artist_repo.upsert(artist)
    log_artist_repo.register_playlist_artist(playlist_id, artist.id)
    return artist


def test_tier1_exact_match() -> None:
    playlist_id = uuid4()
    log_artist_repo = FakeLogArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    # Seed canonical artist
    artist_repo.upsert(Artist(
        id="mbid-metallica", name="Metallica", sort_name="Metallica",
    ))

    _make_pending_artist("METALLICA", log_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        log_artist_repo=log_artist_repo,
        log_identity_repo=FakeLogIdentityRepository(),
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=FakeGlobalMappingRuleRepository(),
        mb_client=StubMbClient(),
    )

    artists = list(log_artist_repo._data.values())
    assert artists[0].match_status == MatchStatus.AUTO_MATCHED


def test_tier3_mb_api_auto_matched() -> None:
    playlist_id = uuid4()
    log_artist_repo = FakeLogArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    _make_pending_artist("OZZY OSBOURNE", log_artist_repo, playlist_id)

    mb_client = StubMbClient({
        "OZZY OSBOURNE": [
            {"id": "mbid-ozzy", "name": "Ozzy Osbourne",
             "sort-name": "Osbourne, Ozzy", "score": 100},
        ]
    })

    match_artists_for_playlist(
        playlist_id=playlist_id,
        log_artist_repo=log_artist_repo,
        log_identity_repo=FakeLogIdentityRepository(),
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=FakeGlobalMappingRuleRepository(),
        mb_client=mb_client,
    )

    artists = list(log_artist_repo._data.values())
    assert artists[0].match_status == MatchStatus.AUTO_MATCHED
    assert artist_repo.get_by_id("mbid-ozzy") is not None


def test_no_match_any_tier_needs_review() -> None:
    playlist_id = uuid4()
    log_artist_repo = FakeLogArtistRepository()

    _make_pending_artist("UNKNOWN BAND XYZ", log_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        log_artist_repo=log_artist_repo,
        log_identity_repo=FakeLogIdentityRepository(),
        artist_repo=FakeArtistRepository(),
        match_repo=FakeMatchRepository(),
        rules_repo=FakeGlobalMappingRuleRepository(),
        mb_client=StubMbClient(),
    )

    artists = list(log_artist_repo._data.values())
    assert artists[0].match_status == MatchStatus.NEEDS_REVIEW


def test_global_rule_exact_match() -> None:
    playlist_id = uuid4()
    log_artist_repo = FakeLogArtistRepository()
    rules_repo = FakeGlobalMappingRuleRepository()
    match_repo = FakeMatchRepository()

    rules_repo.create(GlobalMappingRule(
        id=uuid4(), source_pattern="ac dc",
        target_type=TargetType.ARTIST, target_id="mbid-acdc", priority=10,
    ))

    _make_pending_artist("AC/DC", log_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        log_artist_repo=log_artist_repo,
        log_identity_repo=FakeLogIdentityRepository(),
        artist_repo=FakeArtistRepository(),
        match_repo=match_repo,
        rules_repo=rules_repo,
        mb_client=StubMbClient(),
    )

    artists = list(log_artist_repo._data.values())
    assert artists[0].match_status == MatchStatus.AUTO_MATCHED


def test_cascade_auto_rejected() -> None:
    playlist_id = uuid4()
    log_artist_repo = FakeLogArtistRepository()
    log_identity_repo = FakeLogIdentityRepository()

    artist = _make_pending_artist("BAD ARTIST", log_artist_repo, playlist_id)
    # Manually set to AUTO_REJECTED to test cascade
    log_artist_repo.update_match_status(artist.id, MatchStatus.AUTO_REJECTED)

    from backend.domain.models import LogIdentity
    identity = LogIdentity(
        id=uuid4(), artist_id=artist.id,
        original_title="Song", normalized_title="song",
        normalized_signature="cascade_test_sig_00000000000000",
    )
    log_identity_repo.upsert(identity)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        log_artist_repo=log_artist_repo,
        log_identity_repo=log_identity_repo,
        artist_repo=FakeArtistRepository(),
        match_repo=FakeMatchRepository(),
        rules_repo=FakeGlobalMappingRuleRepository(),
        mb_client=StubMbClient(),
    )

    identities = list(log_identity_repo._data.values())
    assert identities[0].match_status == MatchStatus.AUTO_REJECTED
