"""Characterization tests for `_try_*` functions in identity_matching_service.

Lock the tier-0 (rule) and tier-2 (MBID graph) behavior so PR 3 / PR 4
refactors can detect regressions. Empirical rapidfuzz scores have been
measured against the real implementation and pinned.
"""
from __future__ import annotations

from uuid import UUID, uuid4

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

    lib_repo.upsert(_lib_file(
        "/music/metallica/enormous_sandyman.flac",
        artist_mbid=canonical_mbid,
        track_title="Enormous Sandyman",
    ))

    _try_tier2_mbid_match(
        identity, artist_repo, lib_repo, match_repo, identity_repo
    )

    assert identity_repo._reason_codes.get(identity.id) is None
    assert identity_repo._reason_details.get(identity.id) is None
