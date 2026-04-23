"""Tests for the three ArtistMatchingStrategy implementations.

Strategies produce ArtistMatchResult values; they do not persist. The service
function (PR 4 Task 5) will wire them together and own persistence. These
tests exercise each strategy in isolation using in-memory fakes.
"""
from __future__ import annotations

from uuid import uuid4

from backend.domain.broadcast import BroadcastArtist
from backend.domain.catalog import Artist
from backend.domain.enums import MatchStatus, MatchTier, TargetType
from backend.domain.matching import MappingRule
from backend.services.artist_matching_service import (
    MappingRuleStrategy,
    MusicBrainzApiStrategy,
    NormalizationStrategy,
)
from backend.services.matching_reasons import ReasonCode
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.mb_client import FakeMbClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _broadcast_artist(
    original: str = "Metallica",
    normalized: str | None = None,
) -> BroadcastArtist:
    return BroadcastArtist(
        id=uuid4(),
        original_name=original,
        normalized_name=normalized if normalized is not None else original.lower(),
    )


def _canonical(name: str, artist_id: str | None = None) -> Artist:
    return Artist(
        id=artist_id or str(uuid4()),
        name=name,
        sort_name=name,
    )


def _rule(pattern: str, target_id: str, target_type: TargetType = TargetType.ARTIST) -> MappingRule:
    return MappingRule(
        id=uuid4(),
        source_pattern=pattern,
        target_type=target_type,
        target_id=target_id,
    )


# ---------------------------------------------------------------------------
# MappingRuleStrategy
# ---------------------------------------------------------------------------


def test_mapping_rule_hit() -> None:
    target_id = str(uuid4())
    strategy = MappingRuleStrategy(rules=[_rule("metallica", target_id)])
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.MANUAL
    assert result.confidence_score == 100.0
    assert result.target_id == target_id
    assert result.reason_code is None


def test_mapping_rule_miss_returns_none() -> None:
    strategy = MappingRuleStrategy(rules=[_rule("metallica", str(uuid4()))])
    assert strategy.apply(_broadcast_artist("Foo", "foo")) is None


def test_mapping_rule_skips_non_artist_target_type() -> None:
    strategy = MappingRuleStrategy(
        rules=[_rule("metallica", str(uuid4()), target_type=TargetType.LIBRARY_FILE)]
    )
    assert strategy.apply(_broadcast_artist("Metallica", "metallica")) is None


# ---------------------------------------------------------------------------
# NormalizationStrategy
# ---------------------------------------------------------------------------


def test_normalization_exact_hit_auto_matches() -> None:
    canonical = _canonical("Metallica")
    strategy = NormalizationStrategy(all_canonical=[canonical])
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.NORMALIZATION
    assert result.confidence_score == 100.0
    assert result.target_id == canonical.id
    assert result.reason_code is None


def test_normalization_high_score_with_gap_auto_matches() -> None:
    # Pick a fuzzy near-miss of "metallica"; should still score high AND
    # the runner-up (totally unrelated) should be well below.
    best = _canonical("metallic")
    other = _canonical("radiohead")
    # Lower high_threshold so ~94 score clears the AUTO_MATCHED bar via the
    # "high_threshold + gap" branch rather than the MB_AUTO_LINK_SCORE bypass.
    strategy = NormalizationStrategy(
        all_canonical=[best, other],
        high_threshold=80,
        mb_score_gap=10,
    )
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    # High score + large gap → AUTO_MATCHED
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.NORMALIZATION
    assert result.target_id == best.id
    assert result.confidence_score >= 90


def test_normalization_gap_insufficient_needs_review_ambiguous_gap() -> None:
    # Neither canonical normalizes to the target, but both score high
    # and close. broadcast normalized_name = "metalica" (typo)
    c1 = _canonical("metallica")
    c2 = _canonical("metallicca")
    strategy = NormalizationStrategy(
        all_canonical=[c1, c2],
        high_threshold=80,
        mb_score_gap=50,  # force gap insufficiency
    )
    result = strategy.apply(_broadcast_artist("Metalica", "metalica"))

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.tier == MatchTier.NORMALIZATION
    assert result.reason_code == ReasonCode.AMBIGUOUS_GAP
    assert result.reason_detail is not None


def test_normalization_low_score_needs_review_low_confidence() -> None:
    # Only loosely similar canonicals → top score in the 50-64 band with
    # small gap (no MID_BAND auto-match), forces LOW_CONFIDENCE.
    c1 = _canonical("metal")
    c2 = _canonical("metal band")
    strategy = NormalizationStrategy(all_canonical=[c1, c2])
    result = strategy.apply(_broadcast_artist("Metalheads", "metalheads"))

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.tier == MatchTier.NORMALIZATION
    assert result.reason_code == ReasonCode.LOW_CONFIDENCE


def test_normalization_empty_canonical_returns_none() -> None:
    strategy = NormalizationStrategy(all_canonical=[])
    assert strategy.apply(_broadcast_artist()) is None


# ---------------------------------------------------------------------------
# Review fixes (mirrors identity side: has_competitor, noise floor fall-through,
# separate high_threshold vs MB_AUTO_LINK_SCORE).
# ---------------------------------------------------------------------------


def test_normalization_lone_candidate_below_mid_band_needs_review_not_auto() -> None:
    """Issue #1: a SINGLE canonical scoring in the MID_BAND (55-64) must
    NEEDS_REVIEW, not AUTO_MATCH, because gap=100 is synthetic (no competitor)."""
    # Pick a canonical that fuzzy-scores in MID_BAND (55-64) against the broadcast.
    # "heavy metal" vs "metal" -> ~62.5 (inside MID_BAND), gap synthesized to 100.
    only = _canonical("heavy metal")
    strategy = NormalizationStrategy(all_canonical=[only])
    result = strategy.apply(_broadcast_artist("Metal", "metal"))

    assert result is not None
    # Top score is in MID_BAND but there's no real competitor: must NOT auto-match.
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.reason_code == ReasonCode.LOW_CONFIDENCE


def test_mb_lone_candidate_mid_band_needs_review_not_auto() -> None:
    """Issue #1: a SINGLE MB candidate scoring in MID_BAND must NEEDS_REVIEW,
    not AUTO_MATCH, because second=0 makes gap=top_score (no real competitor)."""
    mb = FakeMbClient(responses={"Foo": [
        {"id": "mbid-only", "name": "Foo Bar", "score": 62},
    ]})
    repo = FakeArtistRepository()
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo)
    result = strategy.apply(_broadcast_artist("Foo", "foo"))

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    # Must not upsert — only AUTO_MATCHED triggers the side effect.
    assert repo.list_all() == []


def test_normalization_below_noise_floor_falls_through_to_next_strategy() -> None:
    """Issue #2: canonicals exist but all fuzzy scores < MIN_PRESENTATION_SCORE
    (50) → apply() returns None so the engine can reach MusicBrainzApi."""
    # These names share almost nothing with "xyzzyqq" — scores will be well below 50.
    c1 = _canonical("metallica")
    c2 = _canonical("radiohead")
    strategy = NormalizationStrategy(all_canonical=[c1, c2])
    result = strategy.apply(_broadcast_artist("Xyzzyqq", "xyzzyqq"))

    assert result is None


def test_normalization_score_90_with_gap_auto_matches_via_high_threshold() -> None:
    """Issue #3: a score of ~90-94 with sufficient gap must AUTO_MATCH via the
    high_threshold(80)+gap(10) band. Previously broken because both ctor params
    were conflated to MB_AUTO_LINK_SCORE=95, so 90 < 95 forced NEEDS_REVIEW."""
    best = _canonical("metallic")  # fuzzy ~94 against "metallica"
    other = _canonical("radiohead")  # far away — gap is large
    # Rely on DEFAULT ctor (no kwargs) so we're testing the default separation
    # of high_threshold=80 vs MB_AUTO_LINK_SCORE=95.
    strategy = NormalizationStrategy(all_canonical=[best, other])
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.target_id == best.id
    # Score should be in the 80-94 band, i.e. strictly below MB_AUTO_LINK_SCORE(95)
    # — so the AUTO decision came through the high_threshold+gap branch.
    assert result.confidence_score < 95


def test_mb_min_candidate_score_constant_lives_in_matching_constants() -> None:
    """Issue #4: MB_MIN_CANDIDATE_SCORE moved from class-level constant to
    matching_constants so all thresholds share one source of truth."""
    from backend.services import matching_constants

    assert hasattr(matching_constants, "MB_MIN_CANDIDATE_SCORE")
    assert matching_constants.MB_MIN_CANDIDATE_SCORE == 60


# ---------------------------------------------------------------------------
# MusicBrainzApiStrategy
# ---------------------------------------------------------------------------


def test_mb_strategy_high_score_auto_matches() -> None:
    mb_id = "mbid-1234"
    mb = FakeMbClient(responses={"Metallica": [
        {"id": mb_id, "name": "Metallica", "sort-name": "Metallica",
         "disambiguation": "US metal band", "score": 100},
        {"id": "mbid-other", "name": "Metallicka", "score": 50},
    ]})
    repo = FakeArtistRepository()
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo)
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.MUSICBRAINZ_API
    assert result.target_id == mb_id
    assert result.confidence_score == 100
    assert result.reason_code is None
    # Side-effect: MB result persisted to local catalog on AUTO_MATCHED.
    stored = repo.get_by_id(mb_id)
    assert stored is not None
    assert stored.name == "Metallica"
    assert stored.disambiguation == "US metal band"


def test_mb_strategy_no_results_returns_none() -> None:
    mb = FakeMbClient(responses={})
    repo = FakeArtistRepository()
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo)
    assert strategy.apply(_broadcast_artist("Unknown", "unknown")) is None


def test_mb_strategy_all_below_60_returns_none() -> None:
    mb = FakeMbClient(responses={"Fuzzy": [
        {"id": "a", "name": "A", "score": 55},
        {"id": "b", "name": "B", "score": 30},
    ]})
    repo = FakeArtistRepository()
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo)
    assert strategy.apply(_broadcast_artist("Fuzzy", "fuzzy")) is None
    # No upsert should have happened.
    assert repo.list_all() == []


def test_mb_strategy_mid_score_needs_review_without_upsert() -> None:
    # Top candidate in 60-94 band with small gap (not in MID_BAND) → NEEDS_REVIEW
    # with LOW_CONFIDENCE, and the strategy must NOT upsert.
    mb = FakeMbClient(responses={"Metallica": [
        {"id": "mbid-top", "name": "Metallica Alt", "score": 70},
        {"id": "mbid-two", "name": "Metallic", "score": 68},
    ]})
    repo = FakeArtistRepository()
    strategy = MusicBrainzApiStrategy(mb_client=mb, artist_repo=repo)
    result = strategy.apply(_broadcast_artist("Metallica", "metallica"))

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.tier == MatchTier.MUSICBRAINZ_API
    assert result.reason_code == ReasonCode.LOW_CONFIDENCE
    assert result.target_id == "mbid-top"
    # NEEDS_REVIEW path must not touch local catalog.
    assert repo.list_all() == []
