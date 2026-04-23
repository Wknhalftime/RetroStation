"""Orchestrator-level tests for `match_artists_for_playlist`.

# PR 4 replaced _try_rule_match / _try_exact_match / _try_fuzzy_match /
# _try_mb_match with the ArtistMatchingEngine Strategy Pattern. Tests that
# pinned the legacy function signatures were either deleted (impl-detail
# tests) or rewritten to assert the same external behavior against the new
# strategies or the rewritten service function. Reason-string baseline
# tests were updated in-place from "no reason persisted" to "ReasonCode
# populated" — documented behavior change, not a regression.

Individual strategy behaviors are covered in test_artist_matching_strategies.py
and the engine's dispatch is covered in test_artist_matching_engine.py. This
file exercises the wiring: strategy order, persistence, no-match fallback,
and the AUTO_REJECTED cascade preserved from the legacy implementation.
"""
from __future__ import annotations

from uuid import uuid4

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.catalog import Artist
from backend.domain.enums import MatchStatus, MatchTier, TargetType
from backend.domain.matching import MappingRule
from backend.services.artist_matching_service import match_artists_for_playlist
from backend.services.matching_constants import MB_AUTO_LINK_SCORE, MB_SCORE_GAP
from backend.services.matching_reasons import ReasonCode
from backend.services.normalization import normalize_artist
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient


def _pending_artist(
    name: str,
    broadcast_artist_repo: FakeBroadcastArtistRepository,
    playlist_id: object,
) -> BroadcastArtist:
    artist = BroadcastArtist(
        id=uuid4(),
        original_name=name,
        normalized_name=normalize_artist(name),
    )
    broadcast_artist_repo.upsert(artist)
    broadcast_artist_repo.register_playlist_artist(playlist_id, artist.id)  # type: ignore[arg-type]
    return artist


# ---------------------------------------------------------------------------
# Replacement orchestrator-level coverage for deleted _try_* characterization
# ---------------------------------------------------------------------------


def test_match_artists_rule_hit_creates_match_with_manual_tier() -> None:
    """Covers old _try_rule_match: a mapping rule hit writes AUTO_MATCHED +
    MANUAL-tier Match row."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    match_repo = FakeMatchRepository()
    rules_repo = FakeMappingRuleRepository()

    artist = _pending_artist("AC/DC", broadcast_artist_repo, playlist_id)
    rules_repo.create(MappingRule(
        id=uuid4(),
        source_pattern=artist.normalized_name,
        target_type=TargetType.ARTIST,
        target_id="mbid-acdc",
        priority=10,
    ))

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=FakeArtistRepository(),
        match_repo=match_repo,
        rules_repo=rules_repo,
        mb_client=FakeMbClient(),
    )

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    created = match_repo.get_by_artist(artist.id)
    assert created is not None
    assert created.target_id == "mbid-acdc"
    assert created.target_type == TargetType.ARTIST
    assert created.confidence_score == 100.0
    assert created.match_tier == MatchTier.MANUAL


def test_match_artists_exact_match_creates_match_with_normalization_tier() -> None:
    """Covers old _try_exact_match: exact normalized-name hit against the
    local canonical catalog writes AUTO_MATCHED + NORMALIZATION-tier Match."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    artist_repo.upsert(Artist(id="mbid-metallica", name="Metallica", sort_name="Metallica"))
    artist = _pending_artist("METALLICA", broadcast_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=FakeMappingRuleRepository(),
        mb_client=FakeMbClient(),
    )

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    created = match_repo.get_by_artist(artist.id)
    assert created is not None
    assert created.target_id == "mbid-metallica"
    assert created.match_tier == MatchTier.NORMALIZATION
    assert created.confidence_score == 100.0


def test_match_artists_fuzzy_mid_persists_low_confidence_reason() -> None:
    """Updated reason-string baseline: a mid-confidence fuzzy hit now
    persists ReasonCode.LOW_CONFIDENCE + a formatted detail string.

    Previously (PR 2 baseline) this was pinned to `_reason_codes.get(...) is
    None` because the legacy fuzzy path never passed reason kwargs. PR 4
    intentionally surfaces the reason through the strategy result.
    """
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    artist_repo.upsert(Artist(id="mbid-metallica", name="Metallica", sort_name="Metallica"))
    artist = _pending_artist("Metalikka", broadcast_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=FakeMappingRuleRepository(),
        mb_client=FakeMbClient(),
        mb_auto_link_score=MB_AUTO_LINK_SCORE,
        mb_score_gap=MB_SCORE_GAP,
    )

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    # No Match row on NEEDS_REVIEW (service only writes Match on AUTO_MATCHED).
    assert match_repo.get_by_artist(artist.id) is None
    # NEW behavior: reason is persisted.
    assert broadcast_artist_repo._reason_codes.get(artist.id) == ReasonCode.LOW_CONFIDENCE
    detail = broadcast_artist_repo._reason_details.get(artist.id)
    assert detail is not None
    assert detail  # non-empty formatted string


def test_match_artists_no_candidates_persists_no_candidates_reason() -> None:
    """When every strategy returns None, the service falls through to
    NEEDS_REVIEW with ReasonCode.NO_CANDIDATES."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()

    artist = _pending_artist("UNKNOWN BAND XYZ", broadcast_artist_repo, playlist_id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=FakeBroadcastTrackIdentityRepository(),
        artist_repo=FakeArtistRepository(),
        match_repo=FakeMatchRepository(),
        rules_repo=FakeMappingRuleRepository(),
        mb_client=FakeMbClient(),
    )

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert broadcast_artist_repo._reason_codes.get(artist.id) == ReasonCode.NO_CANDIDATES
    assert broadcast_artist_repo._reason_details.get(artist.id) is not None


def test_match_artists_mb_hit_upserts_and_creates_match() -> None:
    """Covers old _try_mb_match: MB AUTO_MATCHED upserts the canonical and
    writes a MUSICBRAINZ_API-tier Match row."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    artist_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()

    artist = _pending_artist("OZZY OSBOURNE", broadcast_artist_repo, playlist_id)
    mb_client = FakeMbClient(responses={
        "OZZY OSBOURNE": [
            {"id": "mbid-ozzy", "name": "Ozzy Osbourne",
             "sort-name": "Osbourne, Ozzy", "score": 100},
        ],
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

    stored = broadcast_artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert artist_repo.get_by_id("mbid-ozzy") is not None
    created = match_repo.get_by_artist(artist.id)
    assert created is not None
    assert created.target_id == "mbid-ozzy"
    assert created.match_tier == MatchTier.MUSICBRAINZ_API


def test_match_artists_auto_rejected_cascades_to_identity_bulk_reject() -> None:
    """The AUTO_REJECTED cascade is preserved from legacy: identities under
    an AUTO_REJECTED artist are bulk-rejected at the end of the pass."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    track_identity_repo = FakeBroadcastTrackIdentityRepository()

    artist = _pending_artist("BAD ARTIST", broadcast_artist_repo, playlist_id)
    # Manually force AUTO_REJECTED — simulates a prior run's decision.
    broadcast_artist_repo.update_match_status(artist.id, MatchStatus.AUTO_REJECTED)

    identity = BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=artist.id,
        original_title="Song",
        normalized_title="song",
        normalized_signature="cascade_test_sig_artist_service",
    )
    track_identity_repo.upsert(identity)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=track_identity_repo,
        artist_repo=FakeArtistRepository(),
        match_repo=FakeMatchRepository(),
        rules_repo=FakeMappingRuleRepository(),
        mb_client=FakeMbClient(),
    )

    stored = track_identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_REJECTED
