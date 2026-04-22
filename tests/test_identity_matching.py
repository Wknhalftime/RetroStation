from __future__ import annotations

from uuid import UUID, uuid4

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, TargetType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import Match
from backend.services.identity_matching_service import match_identities_for_playlist
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_artist_with_match(
    artist_name: str,
    canonical_mbid: str,
    broadcast_artist_repo: FakeBroadcastArtistRepository,
    match_repo: FakeMatchRepository,
) -> BroadcastArtist:
    """Create a BroadcastArtist with AUTO_MATCHED status and a Match pointing to canonical MBID."""
    broadcast_artist = BroadcastArtist(
        id=uuid4(),
        original_name=artist_name,
        normalized_name=normalize_artist(artist_name),
        match_status=MatchStatus.AUTO_MATCHED,
    )
    broadcast_artist_repo.upsert(broadcast_artist)

    artist_match = Match(
        id=uuid4(),
        artist_id=broadcast_artist.id,
        target_id=canonical_mbid,
        target_type=TargetType.ARTIST,
        confidence_score=100.0,
        match_tier=MatchTier.MUSICBRAINZ_ID_EXACT,
    )
    match_repo.create(artist_match)
    return broadcast_artist


def _make_identity(
    artist_id: UUID,
    title: str,
    track_identity_repo: FakeBroadcastTrackIdentityRepository,
    playlist_id: UUID,
    artist_normalized_name: str = "",
) -> BroadcastTrackIdentity:
    """Create a PENDING BroadcastTrackIdentity, register it in the playlist."""
    norm_title = normalize_title(title)
    norm_sig = compute_normalized_signature(artist_normalized_name, norm_title)
    identity = BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=artist_id,
        original_title=title,
        normalized_title=norm_title,
        normalized_signature=norm_sig,
        match_status=MatchStatus.PENDING,
    )
    track_identity_repo.upsert(identity)
    track_identity_repo.register_playlist_identity(playlist_id, identity.id)
    return identity


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_tier2_mbid_graph_exact_match() -> None:
    """Tier 2: artist resolved via MBID, library file exists with matching title.

    Score ≥95 → AUTO_MATCHED / MUSICBRAINZ_ID_EXACT.
    """
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    track_identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    library_file_repo = FakeLibraryFileRepository()
    rules_repo = FakeMappingRuleRepository()

    artist_name = "Metallica"
    canonical_mbid = "mbid-metallica"
    track_title = "Enter Sandman"

    broadcast_artist = _setup_artist_with_match(
        artist_name, canonical_mbid, broadcast_artist_repo, match_repo
    )

    identity = _make_identity(
        artist_id=broadcast_artist.id,
        title=track_title,
        track_identity_repo=track_identity_repo,
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
        recording_id="rec-enter-sandman",
        audio=AudioMetadata(
            artist_mbid=canonical_mbid,
            track_title=track_title,
        ),
    )
    library_file_repo.upsert(lib_file)

    work_ids = match_identities_for_playlist(
        playlist_id=playlist_id,
        track_identity_repo=track_identity_repo,
        broadcast_artist_repo=broadcast_artist_repo,
        match_repo=match_repo,
        library_file_repo=library_file_repo,
        rules_repo=rules_repo,
        mb_client=FakeMbClient(),
    )

    # Identity should be AUTO_MATCHED
    updated_identity = track_identity_repo.get_by_id(identity.id)
    assert updated_identity is not None
    assert updated_identity.match_status == MatchStatus.AUTO_MATCHED
    assert updated_identity.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT

    # A Match row should have been created
    identity_match = match_repo.get_by_identity(identity.id)
    assert identity_match is not None
    assert identity_match.library_file_id == lib_file.id
    # PR 3 switched scoring from fuzz.ratio to token_sort_ratio with case-
    # preserving normalization: "enter sandman" vs "Enter Sandman" scores
    # ~84.6 rather than 100. Still AUTO_MATCHED (>=80 with clear gap).
    assert identity_match.confidence_score >= 80

    # recording_id should be in returned work_ids
    assert "rec-enter-sandman" in work_ids


def test_no_library_files_falls_to_needs_review() -> None:
    """Artist is resolved but no library files exist for that MBID → NEEDS_REVIEW / UNCLASSIFIED."""
    playlist_id = uuid4()
    broadcast_artist_repo = FakeBroadcastArtistRepository()
    track_identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    library_file_repo = FakeLibraryFileRepository()
    rules_repo = FakeMappingRuleRepository()

    artist_name = "Ozzy Osbourne"
    canonical_mbid = "mbid-ozzy"
    track_title = "Crazy Train"

    broadcast_artist = _setup_artist_with_match(
        artist_name, canonical_mbid, broadcast_artist_repo, match_repo
    )

    identity = _make_identity(
        artist_id=broadcast_artist.id,
        title=track_title,
        track_identity_repo=track_identity_repo,
        playlist_id=playlist_id,
        artist_normalized_name=normalize_artist(artist_name),
    )

    # No library files seeded

    work_ids = match_identities_for_playlist(
        playlist_id=playlist_id,
        track_identity_repo=track_identity_repo,
        broadcast_artist_repo=broadcast_artist_repo,
        match_repo=match_repo,
        library_file_repo=library_file_repo,
        rules_repo=rules_repo,
        mb_client=FakeMbClient(),
    )

    updated_identity = track_identity_repo.get_by_id(identity.id)
    assert updated_identity is not None
    assert updated_identity.match_status == MatchStatus.NEEDS_REVIEW
    # PR 3: tier now reflects the path taken (ResolvedArtistMbidStrategy ran
    # against a resolved artist with no local files) rather than UNCLASSIFIED.
    assert updated_identity.match_tier == MatchTier.MUSICBRAINZ_ID_SEARCH

    # No Match row created
    assert match_repo.get_by_identity(identity.id) is None
    assert work_ids == []
