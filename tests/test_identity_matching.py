from __future__ import annotations

from uuid import UUID, uuid4

from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, TargetType
from backend.domain.models import LibraryFile, LogArtist, LogIdentity, Match
from backend.services.identity_matching_service import match_identities_for_playlist
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from tests.fakes.global_mapping_rules import FakeGlobalMappingRuleRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.log_artists import FakeLogArtistRepository
from tests.fakes.log_identities import FakeLogIdentityRepository
from tests.fakes.matches import FakeMatchRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_artist_with_match(
    artist_name: str,
    canonical_mbid: str,
    log_artist_repo: FakeLogArtistRepository,
    match_repo: FakeMatchRepository,
) -> LogArtist:
    """Create a LogArtist with AUTO_MATCHED status and a Match pointing to canonical MBID."""
    log_artist = LogArtist(
        id=uuid4(),
        original_name=artist_name,
        normalized_name=normalize_artist(artist_name),
        match_status=MatchStatus.AUTO_MATCHED,
    )
    log_artist_repo.upsert(log_artist)

    artist_match = Match(
        id=uuid4(),
        artist_id=log_artist.id,
        target_id=canonical_mbid,
        target_type=TargetType.ARTIST,
        confidence_score=100.0,
        match_tier=MatchTier.MBID_EXACT,
    )
    match_repo.create(artist_match)
    return log_artist


def _make_identity(
    artist_id: UUID,
    title: str,
    log_identity_repo: FakeLogIdentityRepository,
    playlist_id: UUID,
    artist_normalized_name: str = "",
) -> LogIdentity:
    """Create a PENDING LogIdentity, register it in the playlist."""
    norm_title = normalize_title(title)
    norm_sig = compute_normalized_signature(artist_normalized_name, norm_title)
    identity = LogIdentity(
        id=uuid4(),
        artist_id=artist_id,
        original_title=title,
        normalized_title=norm_title,
        normalized_signature=norm_sig,
        match_status=MatchStatus.PENDING,
    )
    log_identity_repo.upsert(identity)
    log_identity_repo.register_playlist_identity(playlist_id, identity.id)
    return identity


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tier2_mbid_graph_exact_match() -> None:
    """Tier 2: artist resolved via MBID, library file exists with matching title.

    Score ≥95 → AUTO_MATCHED / MBID_EXACT.
    """
    playlist_id = uuid4()
    log_artist_repo = FakeLogArtistRepository()
    log_identity_repo = FakeLogIdentityRepository()
    match_repo = FakeMatchRepository()
    library_file_repo = FakeLibraryFileRepository()
    rules_repo = FakeGlobalMappingRuleRepository()

    artist_name = "Metallica"
    canonical_mbid = "mbid-metallica"
    track_title = "Enter Sandman"

    log_artist = _setup_artist_with_match(
        artist_name, canonical_mbid, log_artist_repo, match_repo
    )

    identity = _make_identity(
        artist_id=log_artist.id,
        title=track_title,
        log_identity_repo=log_identity_repo,
        playlist_id=playlist_id,
        artist_normalized_name=normalize_artist(artist_name),
    )

    # Library file for the same artist/title
    lib_file = LibraryFile(
        id=uuid4(),
        file_path="/music/metallica/enter_sandman.flac",
        file_hash="abc123",
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        artist_mbid=canonical_mbid,
        track_title=track_title,
        recording_id="rec-enter-sandman",
    )
    library_file_repo.upsert(lib_file)

    work_ids = match_identities_for_playlist(
        playlist_id=playlist_id,
        log_identity_repo=log_identity_repo,
        log_artist_repo=log_artist_repo,
        match_repo=match_repo,
        library_file_repo=library_file_repo,
        rules_repo=rules_repo,
    )

    # Identity should be AUTO_MATCHED
    updated_identity = log_identity_repo.get_by_id(identity.id)
    assert updated_identity is not None
    assert updated_identity.match_status == MatchStatus.AUTO_MATCHED
    assert updated_identity.match_tier == MatchTier.MBID_EXACT

    # A Match row should have been created
    identity_match = match_repo.get_by_identity(identity.id)
    assert identity_match is not None
    assert identity_match.library_file_id == lib_file.id
    assert identity_match.confidence_score >= 95

    # recording_id should be in returned work_ids
    assert "rec-enter-sandman" in work_ids


def test_no_library_files_falls_to_needs_review() -> None:
    """Artist is resolved but no library files exist for that MBID → NEEDS_REVIEW / UNKNOWN."""
    playlist_id = uuid4()
    log_artist_repo = FakeLogArtistRepository()
    log_identity_repo = FakeLogIdentityRepository()
    match_repo = FakeMatchRepository()
    library_file_repo = FakeLibraryFileRepository()
    rules_repo = FakeGlobalMappingRuleRepository()

    artist_name = "Ozzy Osbourne"
    canonical_mbid = "mbid-ozzy"
    track_title = "Crazy Train"

    log_artist = _setup_artist_with_match(
        artist_name, canonical_mbid, log_artist_repo, match_repo
    )

    identity = _make_identity(
        artist_id=log_artist.id,
        title=track_title,
        log_identity_repo=log_identity_repo,
        playlist_id=playlist_id,
        artist_normalized_name=normalize_artist(artist_name),
    )

    # No library files seeded

    work_ids = match_identities_for_playlist(
        playlist_id=playlist_id,
        log_identity_repo=log_identity_repo,
        log_artist_repo=log_artist_repo,
        match_repo=match_repo,
        library_file_repo=library_file_repo,
        rules_repo=rules_repo,
    )

    updated_identity = log_identity_repo.get_by_id(identity.id)
    assert updated_identity is not None
    assert updated_identity.match_status == MatchStatus.NEEDS_REVIEW
    assert updated_identity.match_tier == MatchTier.UNKNOWN

    # No Match row created
    assert match_repo.get_by_identity(identity.id) is None
    assert work_ids == []
