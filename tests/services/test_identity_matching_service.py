"""Characterization tests for `_try_*` functions in identity_matching_service.

Lock the tier-0 (rule) and tier-2 (MBID graph) behavior so PR 3 / PR 4
refactors can detect regressions. Empirical rapidfuzz scores have been
measured against the real implementation and pinned.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, TargetType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import MappingRule, Match
from backend.services.identity_matching_service import (
    _try_tier0_rule_match,
    _try_tier2_mbid_match,
)
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository


def _pending_identity(
    artist_id: UUID,
    title: str,
    artist_normalized_name: str,
) -> BroadcastTrackIdentity:
    norm_title = normalize_title(title)
    norm_sig = compute_normalized_signature(artist_normalized_name, norm_title)
    return BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=artist_id,
        original_title=title,
        normalized_title=norm_title,
        normalized_signature=norm_sig,
    )


def _lib_file(
    path: str,
    *,
    artist_mbid: str | None = None,
    track_title: str | None = None,
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
        audio=AudioMetadata(artist_mbid=artist_mbid, track_title=track_title),
    )


# ---------------------------------------------------------------------------
# _try_tier0_rule_match
# ---------------------------------------------------------------------------


def test_characterize_tier0_rule_match_hit_returns_work_id() -> None:
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()

    lib_file = _lib_file(
        "/music/metallica/enter_sandman.flac",
        artist_mbid="mbid-metallica",
        track_title="Enter Sandman",
        work_id="work-enter-sandman",
    )
    lib_repo.upsert(lib_file)

    identity = _pending_identity(
        artist_id=uuid4(),
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    identity_repo.upsert(identity)

    rule = MappingRule(
        id=uuid4(),
        source_pattern=identity.normalized_signature,
        target_type=TargetType.LIBRARY_FILE,
        target_id=lib_file.file_path,
        priority=10,
    )

    result = _try_tier0_rule_match(
        identity, [rule], lib_repo, match_repo, identity_repo
    )

    assert result == "work-enter-sandman"
    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert stored.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT
    created = match_repo.get_by_identity(identity.id)
    assert created is not None
    assert created.library_file_id == lib_file.id
    assert created.confidence_score == 100.0
    assert created.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT


def test_characterize_tier0_rule_match_miss_returns_none() -> None:
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()

    identity = _pending_identity(
        artist_id=uuid4(),
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    identity_repo.upsert(identity)

    result = _try_tier0_rule_match(
        identity, [], lib_repo, match_repo, identity_repo
    )

    assert result is None
    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.PENDING
    assert match_repo.get_by_identity(identity.id) is None


def test_characterize_tier0_rule_match_empty_work_id_returns_empty_string() -> None:
    """Lib file has neither work_id nor recording_id → returns empty string.

    The caller distinguishes "rule hit but no work_id" (empty string,
    truthy-check is False but `is not None`) from "no rule hit" (None).
    """
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()

    lib_file = _lib_file(
        "/music/metallica/enter_sandman.flac",
        artist_mbid="mbid-metallica",
        track_title="Enter Sandman",
        # No work_id and no recording_id.
    )
    lib_repo.upsert(lib_file)

    identity = _pending_identity(
        artist_id=uuid4(),
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    identity_repo.upsert(identity)

    rule = MappingRule(
        id=uuid4(),
        source_pattern=identity.normalized_signature,
        target_type=TargetType.LIBRARY_FILE,
        target_id=lib_file.file_path,
        priority=10,
    )

    result = _try_tier0_rule_match(
        identity, [rule], lib_repo, match_repo, identity_repo
    )

    assert result == ""
    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert stored.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT
    created = match_repo.get_by_identity(identity.id)
    assert created is not None
    assert created.library_file_id == lib_file.id
    assert created.confidence_score == 100.0
    assert created.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT


def test_characterize_tier0_rule_match_hit_returns_recording_id_when_no_work_id() -> None:
    """Lib file has no work_id but has recording_id → returns recording_id.

    Distinct from the empty-string branch: when work_id is absent, the caller
    receives recording_id as the match "handle". Pin this to lock the
    work_id → recording_id → "" fallback chain.
    """
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()

    lib_file = _lib_file(
        "/music/metallica/enter_sandman.flac",
        artist_mbid="mbid-metallica",
        track_title="Enter Sandman",
        recording_id="rec-enter-sandman",
    )
    lib_repo.upsert(lib_file)

    identity = _pending_identity(
        artist_id=uuid4(),
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    identity_repo.upsert(identity)

    rule = MappingRule(
        id=uuid4(),
        source_pattern=identity.normalized_signature,
        target_type=TargetType.LIBRARY_FILE,
        target_id=lib_file.file_path,
        priority=10,
    )

    result = _try_tier0_rule_match(
        identity, [rule], lib_repo, match_repo, identity_repo
    )

    assert result == "rec-enter-sandman"
    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert stored.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT
    created = match_repo.get_by_identity(identity.id)
    assert created is not None
    assert created.library_file_id == lib_file.id
    # Match metadata matches the other tier-0 hit tests: tier-0 always writes
    # MUSICBRAINZ_ID_EXACT at score 100.0, regardless of which fallback
    # (work_id / recording_id / "") the return value takes.
    assert created.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT
    assert created.confidence_score == 100.0


def test_characterize_tier0_rule_match_target_id_as_uuid_uses_get_by_id() -> None:
    """rule.target_id is a valid UUID string → UUID(…) succeeds, get_by_id path runs.

    The service attempts `UUID(rule.target_id)` first and only falls back to
    `get_by_path` on ValueError. This test pins the UUID branch so a refactor
    that drops get_by_id support (or swaps the try/except direction) is
    visible. The existing hit/empty tests exercise the path-fallback branch.
    """
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()

    lib_file = _lib_file(
        "/music/metallica/enter_sandman.flac",
        artist_mbid="mbid-metallica",
        track_title="Enter Sandman",
        work_id="work-enter-sandman",
    )
    lib_repo.upsert(lib_file)

    identity = _pending_identity(
        artist_id=uuid4(),
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    identity_repo.upsert(identity)

    rule = MappingRule(
        id=uuid4(),
        source_pattern=identity.normalized_signature,
        target_type=TargetType.LIBRARY_FILE,
        target_id=str(lib_file.id),  # UUID string, not a file path
        priority=10,
    )

    result = _try_tier0_rule_match(
        identity, [rule], lib_repo, match_repo, identity_repo
    )

    assert result == "work-enter-sandman"
    created = match_repo.get_by_identity(identity.id)
    assert created is not None
    assert created.library_file_id == lib_file.id


# ---------------------------------------------------------------------------
# _try_tier2_mbid_match
# ---------------------------------------------------------------------------
#
# Tier 2 bands (see _try_tier2_mbid_match):
#   score >= 95 → AUTO_MATCHED, MUSICBRAINZ_ID_EXACT
#   score >= 80 → AUTO_MATCHED, NORMALIZATION
#   score >= 60 → NEEDS_REVIEW, NORMALIZATION
#   else         → return None (no side effects)
#
# Empirical rapidfuzz.ratio (both operands already normalized):
#   "enter sandman" vs "enter sandman"        → 100.0  AUTO / MB_ID_EXACT
#   "enter sandman" vs "enter sandmen"        → 92.31  AUTO / NORMALIZATION (80–94)
#   "enter sandman" vs "enormous sandyman"    → 73.33  NEEDS_REVIEW band


def _setup_resolved_artist(
    artist_repo: FakeBroadcastArtistRepository,
    match_repo: FakeMatchRepository,
    canonical_mbid: str,
) -> BroadcastArtist:
    artist = BroadcastArtist(
        id=uuid4(),
        original_name="Metallica",
        normalized_name=normalize_artist("Metallica"),
        match_status=MatchStatus.AUTO_MATCHED,
    )
    artist_repo.upsert(artist)
    match_repo.create(Match(
        id=uuid4(),
        artist_id=artist.id,
        target_id=canonical_mbid,
        target_type=TargetType.ARTIST,
        confidence_score=100.0,
        match_tier=MatchTier.MUSICBRAINZ_ID_EXACT,
    ))
    return artist


def test_characterize_tier2_mbid_match_high_confidence_auto_matches() -> None:
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()

    canonical_mbid = "mbid-metallica"
    broadcast_artist = _setup_resolved_artist(artist_repo, match_repo, canonical_mbid)

    identity = _pending_identity(
        artist_id=broadcast_artist.id,
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    identity_repo.upsert(identity)

    lib_file = _lib_file(
        "/music/metallica/enter_sandman.flac",
        artist_mbid=canonical_mbid,
        track_title="Enter Sandman",
        work_id="work-enter-sandman",
    )
    lib_repo.upsert(lib_file)

    result = _try_tier2_mbid_match(
        identity, artist_repo, lib_repo, match_repo, identity_repo
    )

    assert result is not None
    status, work_id = result
    assert status == MatchStatus.AUTO_MATCHED
    assert work_id == "work-enter-sandman"

    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert stored.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT

    created = match_repo.get_by_identity(identity.id)
    assert created is not None
    assert created.library_file_id == lib_file.id
    assert created.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT
    assert created.confidence_score == 100.0


def test_characterize_tier2_mbid_match_recording_id_fallback_when_no_work_id() -> None:
    """AUTO band, work_id absent, recording_id present → returns recording_id.

    Pins the work_id → recording_id fallback in the AUTO branch of
    _try_tier2_mbid_match. Without this, a refactor that stops falling back
    to recording_id would only be visible through downstream failures.
    """
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()

    canonical_mbid = "mbid-metallica"
    broadcast_artist = _setup_resolved_artist(artist_repo, match_repo, canonical_mbid)

    identity = _pending_identity(
        artist_id=broadcast_artist.id,
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    identity_repo.upsert(identity)

    lib_file = _lib_file(
        "/music/metallica/enter_sandman.flac",
        artist_mbid=canonical_mbid,
        track_title="Enter Sandman",
        recording_id="rec-enter-sandman",
        # No work_id.
    )
    lib_repo.upsert(lib_file)

    result = _try_tier2_mbid_match(
        identity, artist_repo, lib_repo, match_repo, identity_repo
    )

    assert result is not None
    status, work_id = result
    assert status == MatchStatus.AUTO_MATCHED
    assert work_id == "rec-enter-sandman"

    # Anchor side effects: a regression that returns the right tuple but stops
    # persisting the status update / Match row would otherwise pass.
    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert stored.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT
    created = match_repo.get_by_identity(identity.id)
    assert created is not None
    assert created.library_file_id == lib_file.id
    assert created.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT
    assert created.confidence_score == 100.0


def test_characterize_tier2_mbid_match_mid_high_confidence_auto_normalization() -> None:
    """Empirical: ratio("enter sandman", "enter sandmen") = 92.31 → 80–94 band.

    Tier = NORMALIZATION (not MUSICBRAINZ_ID_EXACT), status = AUTO_MATCHED.
    This differs from the >=95 band on tier only — pins the distinct branch so
    PR 3/4 changes to tier/threshold selection are visible.
    """
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()

    canonical_mbid = "mbid-metallica"
    broadcast_artist = _setup_resolved_artist(artist_repo, match_repo, canonical_mbid)

    identity = _pending_identity(
        artist_id=broadcast_artist.id,
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    identity_repo.upsert(identity)

    lib_file = _lib_file(
        "/music/metallica/enter_sandmen.flac",
        artist_mbid=canonical_mbid,
        track_title="Enter Sandmen",
        work_id="work-sandmen",
    )
    lib_repo.upsert(lib_file)

    result = _try_tier2_mbid_match(
        identity, artist_repo, lib_repo, match_repo, identity_repo
    )

    assert result is not None
    status, work_id = result
    assert status == MatchStatus.AUTO_MATCHED
    assert work_id == "work-sandmen"

    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert stored.match_tier == MatchTier.NORMALIZATION

    created = match_repo.get_by_identity(identity.id)
    assert created is not None
    assert created.library_file_id == lib_file.id
    assert created.match_tier == MatchTier.NORMALIZATION
    assert created.confidence_score == pytest.approx(92.31, abs=0.01)


def test_characterize_tier2_mbid_match_mid_confidence_needs_review() -> None:
    """Empirical: ratio("enter sandman", "enormous sandyman") = 73.33 → mid band."""
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()

    canonical_mbid = "mbid-metallica"
    broadcast_artist = _setup_resolved_artist(artist_repo, match_repo, canonical_mbid)

    identity = _pending_identity(
        artist_id=broadcast_artist.id,
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    identity_repo.upsert(identity)

    lib_file = _lib_file(
        "/music/metallica/enormous_sandyman.flac",
        artist_mbid=canonical_mbid,
        track_title="Enormous Sandyman",
        work_id="work-other",
    )
    lib_repo.upsert(lib_file)

    result = _try_tier2_mbid_match(
        identity, artist_repo, lib_repo, match_repo, identity_repo
    )

    assert result is not None
    status, work_id = result
    assert status == MatchStatus.NEEDS_REVIEW
    # Current contract: NEEDS_REVIEW branch returns work_id=None even when the
    # library file has a work_id. Pin this so PR 4 changes are visible.
    assert work_id is None

    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.match_tier == MatchTier.NORMALIZATION

    created = match_repo.get_by_identity(identity.id)
    assert created is not None
    assert created.library_file_id == lib_file.id
    assert created.match_tier == MatchTier.NORMALIZATION
    # Pin the empirical score so scoring drift within the same band is visible.
    assert created.confidence_score == pytest.approx(73.33, abs=0.01)


def test_characterize_tier2_mbid_match_no_files_returns_none() -> None:
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()

    canonical_mbid = "mbid-metallica"
    broadcast_artist = _setup_resolved_artist(artist_repo, match_repo, canonical_mbid)

    identity = _pending_identity(
        artist_id=broadcast_artist.id,
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    identity_repo.upsert(identity)

    # No library files for this MBID.

    result = _try_tier2_mbid_match(
        identity, artist_repo, lib_repo, match_repo, identity_repo
    )

    assert result is None
    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.PENDING
    assert match_repo.get_by_identity(identity.id) is None


def test_characterize_tier2_mbid_match_reason_string_currently_unset() -> None:
    """Baseline: _try_tier2_mbid_match does NOT write reason_code/reason_detail.

    Mid-band outcome (NEEDS_REVIEW) calls update_match_status with only
    (id, status, tier). PR 4 is expected to start populating reasons; when
    that lands, this test fails and the change is visible.
    """
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()

    canonical_mbid = "mbid-metallica"
    broadcast_artist = _setup_resolved_artist(artist_repo, match_repo, canonical_mbid)

    identity = _pending_identity(
        artist_id=broadcast_artist.id,
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    identity_repo.upsert(identity)

    lib_file = _lib_file(
        "/music/metallica/enormous_sandyman.flac",
        artist_mbid=canonical_mbid,
        track_title="Enormous Sandyman",
    )
    lib_repo.upsert(lib_file)

    result = _try_tier2_mbid_match(
        identity, artist_repo, lib_repo, match_repo, identity_repo
    )

    # Anchor the baseline: tier-2 must have actually run (NEEDS_REVIEW band)
    # and produced side effects. Without these pins, this test would silently
    # pass if tier-2 regressed to a no-op returning None.
    assert result is not None
    status, _work_id = result
    assert status == MatchStatus.NEEDS_REVIEW
    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.match_tier == MatchTier.NORMALIZATION
    created = match_repo.get_by_identity(identity.id)
    assert created is not None
    assert created.library_file_id == lib_file.id
    assert created.match_tier == MatchTier.NORMALIZATION

    assert identity_repo._reason_codes.get(identity.id) is None
    assert identity_repo._reason_details.get(identity.id) is None
