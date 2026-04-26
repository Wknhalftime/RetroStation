"""Tests for the three IdentityMatchingStrategy implementations.

Strategies produce IdentityMatchResult values; they do not persist. The
orchestrator (Task 10) wires them together. These tests exercise each
strategy in isolation using in-memory fakes.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import (
    EnrichmentStatus,
    MatchStatus,
    MatchTier,
    ReasonCode,
    TargetType,
)
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import MappingRule, Match
from backend.services.identity_matching_service import (
    BroadcastToLocalStrategy,
    IdentityMappingRuleStrategy,
    ResolvedArtistMbidStrategy,
)
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _identity(
    artist_id: UUID,
    *,
    title: str = "enter sandman",
    signature: str = "metallica|enter sandman",
) -> BroadcastTrackIdentity:
    return BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=artist_id,
        original_title=title,
        normalized_title=title,
        normalized_signature=signature,
    )


def _artist(
    name: str = "metallica",
    status: MatchStatus = MatchStatus.AUTO_MATCHED,
) -> BroadcastArtist:
    return BroadcastArtist(
        id=uuid4(),
        original_name=name,
        normalized_name=name,
        match_status=status,
    )


def _lib_file(
    path: str,
    *,
    track_title: str | None = None,
    artist_mbid: str | None = None,
    artist_name: str | None = None,
    recording_mbid: str | None = None,
    work_id: str | None = None,
    recording_id: str | None = None,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=path,
        file_hash="hash-" + path,
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        recording_id=recording_id,
        work_id=work_id,
        audio=AudioMetadata(
            track_title=track_title,
            artist_mbid=artist_mbid,
            artist_name=artist_name,
            recording_mbid=recording_mbid,
        ),
    )


# ---------------------------------------------------------------------------
# Tier 0 — IdentityMappingRuleStrategy
# ---------------------------------------------------------------------------


def test_tier0_hit_returns_auto_matched() -> None:
    lib_repo = FakeLibraryFileRepository()
    lib_file = _lib_file(
        "/m/es.flac",
        track_title="enter sandman",
        work_id="work-es",
    )
    lib_repo.upsert(lib_file)
    rule = MappingRule(
        id=uuid4(),
        source_pattern="metallica|enter sandman",
        target_type=TargetType.LIBRARY_FILE,
        target_id=str(lib_file.id),
    )
    strategy = IdentityMappingRuleStrategy([rule], lib_repo)

    artist = _artist()
    identity = _identity(artist.id)
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.MUSICBRAINZ_ID_EXACT
    assert result.confidence_score == 100.0
    assert result.library_file_id == lib_file.id
    assert result.work_id == "work-es"


def test_tier0_miss_returns_none() -> None:
    lib_repo = FakeLibraryFileRepository()
    strategy = IdentityMappingRuleStrategy([], lib_repo)
    artist = _artist()
    identity = _identity(artist.id)
    assert strategy.apply(identity, artist) is None


def test_tier0_skips_non_library_file_target_type() -> None:
    lib_repo = FakeLibraryFileRepository()
    rule = MappingRule(
        id=uuid4(),
        source_pattern="metallica|enter sandman",
        target_type=TargetType.ARTIST,
        target_id="mb-xyz",
    )
    strategy = IdentityMappingRuleStrategy([rule], lib_repo)
    artist = _artist()
    identity = _identity(artist.id)
    assert strategy.apply(identity, artist) is None


# ---------------------------------------------------------------------------
# Tier 1 — ResolvedArtistMbidStrategy
# ---------------------------------------------------------------------------


def _seed_artist_match(match_repo: FakeMatchRepository, artist_id: UUID, mbid: str) -> None:
    match_repo.create(Match(
        id=uuid4(),
        artist_id=artist_id,
        confidence_score=100.0,
        match_tier=MatchTier.MUSICBRAINZ_ID_EXACT,
        target_id=mbid,
        target_type=TargetType.ARTIST,
    ))


def test_tier1_gates_on_pending_artist_returns_none() -> None:
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    mb = FakeMbClient()
    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb)

    artist = _artist(status=MatchStatus.PENDING)
    identity = _identity(artist.id)
    assert strategy.apply(identity, artist) is None


def test_tier1_step_a_high_confidence_skips_mb() -> None:
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    mb = FakeMbClient()
    artist = _artist()
    _seed_artist_match(match_repo, artist.id, "mbid-m")
    # Title matches identity exactly so score = 100 — exercises the
    # unconditional MB_AUTO_LINK_SCORE gate, independent of competitor
    # presence. Previous casing mismatch ("Enter Sandman" vs "enter
    # sandman") scored ~85 and relied on the now-removed synthetic-gap
    # auto-match for lone candidates.
    lib_repo.upsert(_lib_file(
        "/m/es.flac", track_title="enter sandman", artist_mbid="mbid-m", work_id="w-es",
    ))

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb)
    identity = _identity(artist.id, title="enter sandman")
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.MUSICBRAINZ_ID_EXACT
    assert mb.calls == []  # no MB call when Step A is conclusive


def test_tier1_step_b_fires_on_mid_confidence_local() -> None:
    """Step A local score in mid band → Step B MB recording search fires."""
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    artist = _artist()
    _seed_artist_match(match_repo, artist.id, "mbid-m")
    # Local file with a partial-match title (scores in 55-64 mid band)
    lib_repo.upsert(_lib_file(
        "/m/mystery.flac",
        track_title="complete unrelated junk words here",
        artist_mbid="mbid-m",
    ))
    # MB returns recording -> that recording maps to a DIFFERENT local file.
    # Note: hit has NO artist_mbid so Step A does not find it; only Step B does.
    hit = _lib_file(
        "/m/real.flac",
        track_title="enter sandman",
        recording_mbid="rec-abc",
    )
    lib_repo.upsert(hit)
    mb = FakeMbClient(
        recording_searches={("mbid-m", "enter sandman"): [{"id": "rec-abc"}]},
    )

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb)
    identity = _identity(artist.id, title="enter sandman")
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.MUSICBRAINZ_ID_SEARCH
    assert result.library_file_id == hit.id


def test_tier1_no_local_files_falls_through_to_mb() -> None:
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    artist = _artist()
    _seed_artist_match(match_repo, artist.id, "mbid-m")
    # No file with matching artist_mbid for Step A,
    # but MB search resolves to a real file by recording_mbid.
    hit = _lib_file(
        "/m/one.flac",
        track_title="enter sandman",
        recording_mbid="rec-abc",
    )
    lib_repo.upsert(hit)
    mb = FakeMbClient(
        recording_searches={("mbid-m", "enter sandman"): [{"id": "rec-abc"}]},
    )

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb)
    identity = _identity(artist.id, title="enter sandman")
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.MUSICBRAINZ_ID_SEARCH
    assert result.library_file_id == hit.id


def test_tier1_mb_inconclusive_returns_no_local_files_reason() -> None:
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    artist = _artist()
    _seed_artist_match(match_repo, artist.id, "mbid-m")
    mb = FakeMbClient()  # no recording_searches => empty list

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb)
    identity = _identity(artist.id, title="enter sandman")
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.reason_code == ReasonCode.NO_LOCAL_FILES
    assert result.library_file_id is None


def test_tier1_missing_match_record_returns_missing_match_record_reason() -> None:
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()  # no artist match seeded
    mb = FakeMbClient()

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb)
    artist = _artist(status=MatchStatus.AUTO_MATCHED)
    identity = _identity(artist.id)
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.reason_code == ReasonCode.MISSING_MATCH_RECORD


# ---------------------------------------------------------------------------
# Tier 2 — BroadcastToLocalStrategy
# ---------------------------------------------------------------------------


def test_tier2_gates_on_resolved_artist_returns_none() -> None:
    lib_repo = FakeLibraryFileRepository()
    strategy = BroadcastToLocalStrategy(lib_repo)
    artist = _artist(status=MatchStatus.AUTO_MATCHED)
    identity = _identity(artist.id)
    assert strategy.apply(identity, artist) is None


def test_tier2_high_confidence_auto_matched() -> None:
    lib_repo = FakeLibraryFileRepository()
    # Exact title match so score = 100 — clears the unconditional
    # MB_AUTO_LINK_SCORE gate without depending on has_competitor.
    hit = _lib_file(
        "/m/es.flac",
        track_title="enter sandman",
        artist_name="metallica",
    )
    lib_repo.upsert(hit)
    strategy = BroadcastToLocalStrategy(lib_repo)

    artist = _artist(status=MatchStatus.PENDING)
    identity = _identity(artist.id, title="enter sandman")
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.LOCAL_FILE_FUZZY
    assert result.library_file_id == hit.id


def test_tier2_no_candidates_returns_no_candidates_reason() -> None:
    lib_repo = FakeLibraryFileRepository()
    strategy = BroadcastToLocalStrategy(lib_repo)

    artist = _artist(name="obscure", status=MatchStatus.PENDING)
    identity = _identity(artist.id)
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.reason_code == ReasonCode.NO_CANDIDATES
    assert result.library_file_id is None


def test_tier2_mid_confidence_needs_review_with_low_confidence_reason() -> None:
    """Single candidate with low-similarity title lands in NEEDS_REVIEW."""
    lib_repo = FakeLibraryFileRepository()
    lib_repo.upsert(_lib_file(
        "/m/x.flac",
        track_title="completely different song words xyz",
        artist_name="metallica",
    ))
    strategy = BroadcastToLocalStrategy(lib_repo)

    artist = _artist(status=MatchStatus.PENDING)
    identity = _identity(artist.id, title="enter sandman")
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.reason_code == ReasonCode.LOW_CONFIDENCE


# ---------------------------------------------------------------------------
# Lone-candidate high-band guard (correctness review follow-up)
# ---------------------------------------------------------------------------


def test_tier2_lone_candidate_high_band_needs_review_not_auto() -> None:
    """BroadcastToLocalStrategy must NOT auto-match when a single library
    file scores above high_threshold (80) but below MB_AUTO_LINK_SCORE (95).
    Without has_competitor on the high_threshold+gap clause, synthesized
    gap=100 would falsely trigger auto-match for a lone result.
    """
    artist = _artist(status=MatchStatus.PENDING)
    identity = _identity(
        artist.id, title="enter sandman", signature="metallica|enter sandman",
    )
    # Score ~90 vs "enter sandman" — above 80, below 95.
    lib_repo = FakeLibraryFileRepository()
    lib_repo.upsert(
        _lib_file(
            "/m.flac", track_title="Enter Sandmen",  # one-letter diff -> ~92
            artist_name="metallica",
        )
    )

    strat = BroadcastToLocalStrategy(lib_repo, high_threshold=80)
    result = strat.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    # Reason is LOW_CONFIDENCE, not AMBIGUOUS_GAP — no peer for ambiguity.
    assert result.reason_code == ReasonCode.LOW_CONFIDENCE


def test_tier2_lone_candidate_above_auto_link_still_auto_matches() -> None:
    """The unconditional-AUTO gate (>= 95) intentionally allows lone
    matches through. This test pins the asymmetry so the guard can't be
    over-applied to the top band in a later refactor.
    """
    artist = _artist(status=MatchStatus.PENDING)
    identity = _identity(
        artist.id, title="enter sandman", signature="metallica|enter sandman",
    )
    lib_repo = FakeLibraryFileRepository()
    # Exact title match -> score 100
    lib_repo.upsert(
        _lib_file(
            "/m.flac", track_title="enter sandman", artist_name="metallica",
        )
    )

    strat = BroadcastToLocalStrategy(lib_repo, high_threshold=80)
    result = strat.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
