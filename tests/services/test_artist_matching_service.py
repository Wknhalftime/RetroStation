"""Characterization tests for `_try_*` functions in artist_matching_service.

These lock current observable behavior so PR 3 / PR 4 refactors can detect
regressions. They assert what the code actually does today, not what any
spec wishes it did. Empirical confidence scores were measured against the
real rapidfuzz implementation and pinned.
"""
from __future__ import annotations

from uuid import uuid4

from backend.domain.broadcast import BroadcastArtist
from backend.domain.catalog import Artist
from backend.domain.enums import MatchStatus, MatchTier, TargetType
from backend.domain.matching import MappingRule
from backend.services.artist_matching_service import (
    _try_exact_match,
    _try_fuzzy_match,
    _try_mb_match,
    _try_rule_match,
)
from backend.services.normalization import normalize_artist
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient


def _pending_artist(name: str) -> BroadcastArtist:
    return BroadcastArtist(
        id=uuid4(),
        original_name=name,
        normalized_name=normalize_artist(name),
    )


# ---------------------------------------------------------------------------
# _try_rule_match
# ---------------------------------------------------------------------------


def test_characterize_try_rule_match_hit() -> None:
    artist_repo = FakeBroadcastArtistRepository()
    match_repo = FakeMatchRepository()
    artist = _pending_artist("AC/DC")
    artist_repo.upsert(artist)

    rule = MappingRule(
        id=uuid4(),
        source_pattern=artist.normalized_name,
        target_type=TargetType.ARTIST,
        target_id="mbid-acdc",
        priority=10,
    )

    result = _try_rule_match(artist, [rule], artist_repo, match_repo)

    assert result is True
    stored = artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    created = match_repo.get_by_artist(artist.id)
    assert created is not None
    assert created.target_id == "mbid-acdc"
    assert created.target_type == TargetType.ARTIST
    assert created.confidence_score == 100.0
    assert created.match_tier == MatchTier.MANUAL


def test_characterize_try_rule_match_miss_returns_false_no_side_effects() -> None:
    artist_repo = FakeBroadcastArtistRepository()
    match_repo = FakeMatchRepository()
    artist = _pending_artist("AC/DC")
    artist_repo.upsert(artist)

    result = _try_rule_match(artist, [], artist_repo, match_repo)

    assert result is False
    stored = artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.PENDING
    assert match_repo.get_by_artist(artist.id) is None


def test_characterize_try_rule_match_skips_non_artist_target_type() -> None:
    artist_repo = FakeBroadcastArtistRepository()
    match_repo = FakeMatchRepository()
    artist = _pending_artist("AC/DC")
    artist_repo.upsert(artist)

    rule = MappingRule(
        id=uuid4(),
        source_pattern=artist.normalized_name,
        target_type=TargetType.LIBRARY_FILE,
        target_id="some-library-file-path",
        priority=10,
    )

    result = _try_rule_match(artist, [rule], artist_repo, match_repo)

    assert result is False
    stored = artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.PENDING
    assert match_repo.get_by_artist(artist.id) is None


# ---------------------------------------------------------------------------
# _try_exact_match
# ---------------------------------------------------------------------------


def test_characterize_try_exact_match_hit() -> None:
    artist_repo = FakeBroadcastArtistRepository()
    match_repo = FakeMatchRepository()
    artist = _pending_artist("METALLICA")
    artist_repo.upsert(artist)

    canonical = Artist(id="mbid-metallica", name="Metallica", sort_name="Metallica")

    result = _try_exact_match(artist, [canonical], artist_repo, match_repo)

    assert result is True
    stored = artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    created = match_repo.get_by_artist(artist.id)
    assert created is not None
    assert created.target_id == "mbid-metallica"
    assert created.target_type == TargetType.ARTIST
    assert created.confidence_score == 100.0
    assert created.match_tier == MatchTier.NORMALIZATION


def test_characterize_try_exact_match_miss_returns_false() -> None:
    artist_repo = FakeBroadcastArtistRepository()
    match_repo = FakeMatchRepository()
    artist = _pending_artist("METALLICA")
    artist_repo.upsert(artist)

    canonical = Artist(id="mbid-other", name="Some Other Band", sort_name="Other")

    result = _try_exact_match(artist, [canonical], artist_repo, match_repo)

    assert result is False
    stored = artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.PENDING
    assert match_repo.get_by_artist(artist.id) is None


# ---------------------------------------------------------------------------
# _try_fuzzy_match
# ---------------------------------------------------------------------------
#
# Score thresholds (see _apply_thresholds in artist_matching_service.py):
#   score >= auto_link_score (95) AND gap >= score_gap (10)  → AUTO_MATCHED
#   score >= auto_link_score (95) AND gap <  score_gap (10)  → NEEDS_REVIEW
#   score >= 80                                               → AUTO_MATCHED
#   score >= 60                                               → NEEDS_REVIEW
#   else                                                      → return None/None
#
# Empirical rapidfuzz.token_sort_ratio results (both operands already
# normalized — see normalize_artist):
#   "metalica"  vs "metallica"  → 94.117... (int → 94)   AUTO band (80–94)
#   "metalikka" vs "metallica"  → 77.777... (int → 77)   NEEDS_REVIEW (60–79)


def test_characterize_try_fuzzy_match_high_confidence_auto_matches() -> None:
    """Score 94 (int cast) → AUTO_MATCHED at the 80–94 band with NORMALIZATION tier.

    Score 94 is below auto_link_score=95, so the >=80 branch of
    `_apply_thresholds` fires. The Match row stores the int-cast score.
    """
    artist_repo = FakeBroadcastArtistRepository()
    match_repo = FakeMatchRepository()
    artist = _pending_artist("Metalica")
    artist_repo.upsert(artist)

    canonical = Artist(id="mbid-metallica", name="Metallica", sort_name="Metallica")

    result = _try_fuzzy_match(
        artist, [canonical], artist_repo, match_repo,
        mb_auto_link_score=95, mb_score_gap=10,
    )

    assert result is True
    stored = artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    created = match_repo.get_by_artist(artist.id)
    assert created is not None
    assert created.target_id == "mbid-metallica"
    assert created.target_type == TargetType.ARTIST
    assert created.match_tier == MatchTier.NORMALIZATION
    # int(94.117...) == 94; stored on the Match as-is.
    assert created.confidence_score == 94


def test_characterize_try_fuzzy_match_high_score_insufficient_gap_needs_review() -> None:
    """Both canonicals score >=95 with gap < score_gap → NEEDS_REVIEW, no Match.

    Pins the `score >= auto_link_score AND gap < score_gap` branch of
    `_apply_thresholds` on the fuzzy call path. Two identical canonical names
    against the same query give score=100, gap=0 — firmly in this branch.
    """
    artist_repo = FakeBroadcastArtistRepository()
    match_repo = FakeMatchRepository()
    artist = _pending_artist("Metallica")
    artist_repo.upsert(artist)

    canonical_a = Artist(id="mbid-metallica-a", name="Metallica", sort_name="Metallica")
    canonical_b = Artist(id="mbid-metallica-b", name="Metallica", sort_name="Metallica")

    result = _try_fuzzy_match(
        artist, [canonical_a, canonical_b], artist_repo, match_repo,
        mb_auto_link_score=95, mb_score_gap=10,
    )

    assert result is True
    stored = artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    # Current behavior: high-score-but-ambiguous yields NO Match row.
    assert match_repo.get_by_artist(artist.id) is None


def test_characterize_try_fuzzy_match_mid_confidence_needs_review() -> None:
    """Score 77 → NEEDS_REVIEW band (60–79); NO Match row is written."""
    artist_repo = FakeBroadcastArtistRepository()
    match_repo = FakeMatchRepository()
    artist = _pending_artist("Metalikka")
    artist_repo.upsert(artist)

    canonical = Artist(id="mbid-metallica", name="Metallica", sort_name="Metallica")

    result = _try_fuzzy_match(
        artist, [canonical], artist_repo, match_repo,
        mb_auto_link_score=95, mb_score_gap=10,
    )

    assert result is True
    stored = artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    # Current behavior: no Match row on NEEDS_REVIEW from fuzzy.
    assert match_repo.get_by_artist(artist.id) is None


def test_characterize_try_fuzzy_match_reason_string_currently_unset() -> None:
    """Baseline: _try_fuzzy_match does NOT pass reason_code/reason_detail today.

    When PR 4 introduces reason writes, this test will fail and the change
    will be visible. Locking the baseline so a silent regression here is
    impossible.
    """
    artist_repo = FakeBroadcastArtistRepository()
    match_repo = FakeMatchRepository()
    artist = _pending_artist("Metalikka")
    artist_repo.upsert(artist)

    canonical = Artist(id="mbid-metallica", name="Metallica", sort_name="Metallica")

    _try_fuzzy_match(
        artist, [canonical], artist_repo, match_repo,
        mb_auto_link_score=95, mb_score_gap=10,
    )

    # update_match_status was called with only (id, status) — the reason kwargs
    # default to None, and the fake stores whatever the caller passed.
    assert artist_repo._reason_codes.get(artist.id) is None
    assert artist_repo._reason_details.get(artist.id) is None


# ---------------------------------------------------------------------------
# _try_mb_match
# ---------------------------------------------------------------------------


def test_characterize_try_mb_match_hit() -> None:
    artist_repo = FakeBroadcastArtistRepository()
    catalog_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()
    artist = _pending_artist("OZZY OSBOURNE")
    artist_repo.upsert(artist)

    mb_client = FakeMbClient(responses={
        "OZZY OSBOURNE": [
            {
                "id": "mbid-ozzy",
                "name": "Ozzy Osbourne",
                "sort-name": "Osbourne, Ozzy",
                "score": 100,
            },
        ],
    })

    result = _try_mb_match(
        artist, mb_client, catalog_repo, artist_repo, match_repo,
        mb_auto_link_score=95, mb_score_gap=10,
    )

    assert result is True
    stored = artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    # MB artist is upserted into the canonical catalog.
    assert catalog_repo.get_by_id("mbid-ozzy") is not None
    created = match_repo.get_by_artist(artist.id)
    assert created is not None
    assert created.target_id == "mbid-ozzy"
    assert created.target_type == TargetType.ARTIST
    assert created.match_tier == MatchTier.MUSICBRAINZ_API
    assert created.confidence_score == 100


def test_characterize_try_mb_match_high_score_insufficient_gap_needs_review() -> None:
    """Top MB candidate >= auto_link_score but gap < mb_score_gap.

    Pins the distinct NEEDS_REVIEW/NORMALIZATION branch of `_apply_thresholds`
    on the MB call path: the canonical artist IS upserted into the catalog
    and status IS updated, but NO Match row is written. Without this pin,
    a refactor that starts writing a Match row here would go undetected.
    """
    artist_repo = FakeBroadcastArtistRepository()
    catalog_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()
    artist = _pending_artist("OZZY OSBOURNE")
    artist_repo.upsert(artist)

    mb_client = FakeMbClient(responses={
        "OZZY OSBOURNE": [
            {"id": "mbid-ozzy-a", "name": "Ozzy Osbourne",
             "sort-name": "Osbourne, Ozzy", "score": 97},
            {"id": "mbid-ozzy-b", "name": "Ozzy Osbourne (tribute)",
             "sort-name": "Osbourne, Ozzy", "score": 96},
        ],
    })

    result = _try_mb_match(
        artist, mb_client, catalog_repo, artist_repo, match_repo,
        mb_auto_link_score=95, mb_score_gap=10,
    )

    assert result is True
    stored = artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    # Current behavior: canonical IS upserted even when status is NEEDS_REVIEW.
    assert catalog_repo.get_by_id("mbid-ozzy-a") is not None
    # Current behavior: NO Match row on NEEDS_REVIEW from MB.
    assert match_repo.get_by_artist(artist.id) is None


def test_characterize_try_mb_match_miss() -> None:
    artist_repo = FakeBroadcastArtistRepository()
    catalog_repo = FakeArtistRepository()
    match_repo = FakeMatchRepository()
    artist = _pending_artist("UNKNOWN BAND XYZ")
    artist_repo.upsert(artist)

    mb_client = FakeMbClient(responses={})  # empty results

    result = _try_mb_match(
        artist, mb_client, catalog_repo, artist_repo, match_repo,
        mb_auto_link_score=95, mb_score_gap=10,
    )

    assert result is False
    stored = artist_repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.PENDING
    assert catalog_repo.list_all() == []
    assert match_repo.get_by_artist(artist.id) is None
