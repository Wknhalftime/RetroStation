"""Service-level tests for match_identities_for_playlist (post PR 3).

PR 3 replaced _try_tier0_rule_match and _try_tier2_mbid_match with the
Strategy Pattern engine. Tests that pinned the legacy function signatures
were either deleted (impl-detail tests) or rewritten to assert the same
external behavior against the new strategy classes. Reason-string baseline
tests were updated in-place from "no reason persisted" to "ReasonCode
populated" — documented behavior change, not a regression.

The per-strategy external contracts (Tier 0 rule hit, Tier 1 MBID fast path,
Tier 2 fuzzy fallback) live in tests/services/test_identity_matching_strategies.py.
This file covers the orchestrator's end-to-end behavior: persistence,
work_id collection, and reason-code propagation.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, ReasonCode, TargetType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import MappingRule, Match
from backend.services.identity_matching_service import match_identities_for_playlist
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient


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
    normalized_title: str | None = None,
    work_id: str | None = None,
    recording_id: str | None = None,
) -> LibraryFile:
    """Build a LibraryFile fixture matching how library_scan_service populates rows.

    When normalized_title is omitted, default-compute it from track_title via
    normalize_title() — exactly what the scanner does at ingest. Tests that
    want to exercise the legacy-no-precomputed-normalized-title path can pass
    normalized_title="" or override per-test.
    """
    if normalized_title is None and track_title is not None:
        normalized_title = normalize_title(track_title)
    return LibraryFile(
        id=uuid4(),
        file_path=path,
        file_hash="hash-" + path,
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        recording_id=recording_id,
        work_id=work_id,
        audio=AudioMetadata(
            artist_mbid=artist_mbid,
            track_title=track_title,
            normalized_title=normalized_title,
        ),
    )


def _register_pending_for_playlist(
    identity_repo: FakeBroadcastTrackIdentityRepository,
    identity: BroadcastTrackIdentity,
    playlist_id: UUID,
) -> None:
    """Upsert identity + register it as PENDING for the given playlist.

    FakeBroadcastTrackIdentityRepository.get_pending_for_playlist() reads
    from an internal playlist-index, so just upserting is not enough.
    """
    identity_repo.upsert(identity)
    identity_repo.register_playlist_identity(playlist_id, identity.id)


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


# ---------------------------------------------------------------------------
# Orchestrator behavior
# ---------------------------------------------------------------------------


def test_match_identities_for_playlist_tier0_rule_hit_collects_work_id() -> None:
    """Rule hit → AUTO_MATCHED, Match row persisted, work_id collected."""
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()
    rules_repo = FakeMappingRuleRepository()
    mb_client = FakeMbClient()

    artist = BroadcastArtist(
        id=uuid4(),
        original_name="Metallica",
        normalized_name=normalize_artist("Metallica"),
    )
    artist_repo.upsert(artist)

    lib_file = _lib_file(
        "/music/metallica/enter_sandman.flac",
        track_title="Enter Sandman",
        work_id="work-enter-sandman",
    )
    lib_repo.upsert(lib_file)

    identity = _pending_identity(
        artist_id=artist.id,
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    playlist_id = uuid4()
    _register_pending_for_playlist(identity_repo, identity, playlist_id)

    rules_repo.create(MappingRule(
        id=uuid4(),
        source_pattern=identity.normalized_signature,
        target_type=TargetType.LIBRARY_FILE,
        target_id=lib_file.file_path,
        priority=10,
    ))

    work_ids = match_identities_for_playlist(
        playlist_id=playlist_id,
        track_identity_repo=identity_repo,
        broadcast_artist_repo=artist_repo,
        match_repo=match_repo,
        library_file_repo=lib_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
        catalog_repo=FakeArtistRepository(),
    )

    assert work_ids == ["work-enter-sandman"]
    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert stored.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT
    created = match_repo.get_by_identity(identity.id)
    assert created is not None
    assert created.library_file_id == lib_file.id
    assert created.work_id == "work-enter-sandman"
    assert created.confidence_score == 100.0
    assert created.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT


def test_match_identities_tier1_mbid_fast_path_collects_work_id() -> None:
    """Resolved artist + matching local file → AUTO_MATCHED, work_id collected."""
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()
    rules_repo = FakeMappingRuleRepository()
    mb_client = FakeMbClient()

    canonical_mbid = "mbid-metallica"
    artist = _setup_resolved_artist(artist_repo, match_repo, canonical_mbid)

    lib_file = _lib_file(
        "/music/metallica/enter_sandman.flac",
        artist_mbid=canonical_mbid,
        # Lowercase matches the normalized_title so score = 100 — the
        # unconditional MB_AUTO_LINK_SCORE gate carries lone candidates
        # regardless of has_competitor. Previous "Enter Sandman" scored
        # ~85 and relied on the now-removed synthetic-gap auto-match.
        track_title="enter sandman",
        work_id="work-enter-sandman",
    )
    lib_repo.upsert(lib_file)

    identity = _pending_identity(
        artist_id=artist.id,
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    playlist_id = uuid4()
    _register_pending_for_playlist(identity_repo, identity, playlist_id)

    work_ids = match_identities_for_playlist(
        playlist_id=playlist_id,
        track_identity_repo=identity_repo,
        broadcast_artist_repo=artist_repo,
        match_repo=match_repo,
        library_file_repo=lib_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
        catalog_repo=FakeArtistRepository(),
    )

    assert work_ids == ["work-enter-sandman"]
    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    persisted = match_repo.get_by_identity(identity.id)
    assert persisted is not None
    assert persisted.work_id == "work-enter-sandman"


# ---------------------------------------------------------------------------
# work_id persistence on the matches row (migration 0024)
# ---------------------------------------------------------------------------
#
# _score_candidates returns IdentityMatchResult.work_id from best_file.work_id
# only — empty string when the file has no work_id, never recording_id as a
# stand-in (matches.work_id is FK to works(id); a recording_id would fail
# the FK on insert). The auto-matcher passes that through to Match.work_id
# and the repository layer normalizes "" -> None at the persistence boundary.
# These three tests pin: work_id present, only recording_id present (still
# persisted as NULL), and neither present (NULL).


def test_match_identities_persists_work_id_when_lib_file_has_work_id() -> None:
    """LibraryFile.work_id is set → Match.work_id matches."""
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()
    rules_repo = FakeMappingRuleRepository()
    mb_client = FakeMbClient()

    canonical_mbid = "mbid-metallica"
    artist = _setup_resolved_artist(artist_repo, match_repo, canonical_mbid)

    lib_file = _lib_file(
        "/music/metallica/enter_sandman.flac",
        artist_mbid=canonical_mbid,
        track_title="enter sandman",
        work_id="work-enter-sandman",
        recording_id="recording-enter-sandman",  # both set; work_id wins
    )
    lib_repo.upsert(lib_file)

    identity = _pending_identity(
        artist_id=artist.id,
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    playlist_id = uuid4()
    _register_pending_for_playlist(identity_repo, identity, playlist_id)

    match_identities_for_playlist(
        playlist_id=playlist_id,
        track_identity_repo=identity_repo,
        broadcast_artist_repo=artist_repo,
        match_repo=match_repo,
        library_file_repo=lib_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
        catalog_repo=FakeArtistRepository(),
    )

    persisted = match_repo.get_by_identity(identity.id)
    assert persisted is not None
    assert persisted.work_id == "work-enter-sandman"


def test_match_identities_persists_null_work_id_when_only_recording_id_set() -> None:
    """LibraryFile.work_id NULL with only recording_id set → Match.work_id is
    None.

    matches.work_id is FK to works(id); a recording_id is not a work_id and
    would fail the FK if persisted. Migration 0011 already backfills
    library_files.work_id from recordings.work_id at scan time, so a NULL
    lf.work_id at this point means the underlying work is genuinely unknown
    (the recording either has no work_id or is detached) — NULL is the
    correct persisted value, not the recording_id.
    """
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()
    rules_repo = FakeMappingRuleRepository()
    mb_client = FakeMbClient()

    canonical_mbid = "mbid-metallica"
    artist = _setup_resolved_artist(artist_repo, match_repo, canonical_mbid)

    lib_file = _lib_file(
        "/music/metallica/enter_sandman.flac",
        artist_mbid=canonical_mbid,
        track_title="enter sandman",
        work_id=None,
        recording_id="recording-enter-sandman",
    )
    lib_repo.upsert(lib_file)

    identity = _pending_identity(
        artist_id=artist.id,
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    playlist_id = uuid4()
    _register_pending_for_playlist(identity_repo, identity, playlist_id)

    match_identities_for_playlist(
        playlist_id=playlist_id,
        track_identity_repo=identity_repo,
        broadcast_artist_repo=artist_repo,
        match_repo=match_repo,
        library_file_repo=lib_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
        catalog_repo=FakeArtistRepository(),
    )

    persisted = match_repo.get_by_identity(identity.id)
    assert persisted is not None
    assert persisted.work_id is None


def test_match_identities_persists_null_work_id_when_neither_present() -> None:
    """Neither work_id nor recording_id is set → Match.work_id is None
    (NOT empty string — would violate the works(id) FK).
    """
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()
    rules_repo = FakeMappingRuleRepository()
    mb_client = FakeMbClient()

    canonical_mbid = "mbid-metallica"
    artist = _setup_resolved_artist(artist_repo, match_repo, canonical_mbid)

    lib_file = _lib_file(
        "/music/metallica/enter_sandman.flac",
        artist_mbid=canonical_mbid,
        track_title="enter sandman",
        work_id=None,
        recording_id=None,
    )
    lib_repo.upsert(lib_file)

    identity = _pending_identity(
        artist_id=artist.id,
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    playlist_id = uuid4()
    _register_pending_for_playlist(identity_repo, identity, playlist_id)

    match_identities_for_playlist(
        playlist_id=playlist_id,
        track_identity_repo=identity_repo,
        broadcast_artist_repo=artist_repo,
        match_repo=match_repo,
        library_file_repo=lib_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
        catalog_repo=FakeArtistRepository(),
    )

    persisted = match_repo.get_by_identity(identity.id)
    assert persisted is not None
    assert persisted.work_id is None


def test_match_identities_tier1_case_mismatched_track_title_auto_matches() -> None:
    """Regression: broadcast 'Halo' (normalized 'halo') vs library track_title
    'Halo' must AUTO_MATCH at 100, not NEEDS_REVIEW at 75.

    rapidfuzz.fuzz.token_sort_ratio is case-sensitive when no processor is
    supplied. Before _score_candidates was symmetrized to use
    audio.normalized_title (lowercase, accent-stripped via normalize_title),
    the broadcast side arrived as 'halo' while the library side stayed 'Halo',
    yielding 2*3/(4+4) = 75% — below MB_AUTO_LINK_SCORE (95) and below
    strong_match_threshold (80) — so the identity sat in needs_review forever
    despite the artist being cleanly resolved and the file being a perfect
    title match. The fixture omits normalized_title so _lib_file's default
    (normalize_title(track_title)) populates it the way the scanner does.
    """
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()
    rules_repo = FakeMappingRuleRepository()
    mb_client = FakeMbClient()

    canonical_mbid = "mbid-soil"
    artist = _setup_resolved_artist(artist_repo, match_repo, canonical_mbid)

    lib_file = _lib_file(
        "/music/soil/halo.flac",
        artist_mbid=canonical_mbid,
        track_title="Halo",  # raw library tag preserves case
        work_id="work-halo",
    )
    lib_repo.upsert(lib_file)

    identity = _pending_identity(
        artist_id=artist.id,
        title="Halo",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    playlist_id = uuid4()
    _register_pending_for_playlist(identity_repo, identity, playlist_id)

    match_identities_for_playlist(
        playlist_id=playlist_id,
        track_identity_repo=identity_repo,
        broadcast_artist_repo=artist_repo,
        match_repo=match_repo,
        library_file_repo=lib_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
        catalog_repo=FakeArtistRepository(),
    )

    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert stored.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT
    persisted = match_repo.get_by_identity(identity.id)
    assert persisted is not None
    assert persisted.confidence_score == 100.0
    assert persisted.library_file_id == lib_file.id


def test_match_identities_legacy_null_normalized_title_falls_back() -> None:
    """When audio.normalized_title is NULL (legacy / pre-backfill row),
    _candidate_canonical_title falls back to normalize_title(track_title) so
    the case-mismatch scoring bug stays fixed even for un-backfilled rows.
    """
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()
    rules_repo = FakeMappingRuleRepository()
    mb_client = FakeMbClient()

    canonical_mbid = "mbid-soil"
    artist = _setup_resolved_artist(artist_repo, match_repo, canonical_mbid)

    lib_file = _lib_file(
        "/music/soil/halo.flac",
        artist_mbid=canonical_mbid,
        track_title="Halo",
        normalized_title="",  # explicit override: simulate legacy NULL
        work_id="work-halo",
    )
    lib_repo.upsert(lib_file)

    identity = _pending_identity(
        artist_id=artist.id,
        title="Halo",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    playlist_id = uuid4()
    _register_pending_for_playlist(identity_repo, identity, playlist_id)

    match_identities_for_playlist(
        playlist_id=playlist_id,
        track_identity_repo=identity_repo,
        broadcast_artist_repo=artist_repo,
        match_repo=match_repo,
        library_file_repo=lib_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
        catalog_repo=FakeArtistRepository(),
    )

    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED


def test_match_identities_needs_review_persists_reason_code() -> None:
    """Updated behavior: NEEDS_REVIEW outcomes now persist reason_code/detail.

    Prior baseline was "no reason persisted" (legacy _try_tier2_mbid_match
    called update_match_status with just (id, status, tier)). PR 3 surfaces
    the reason metadata from IdentityMatchResult through to the repo.
    """
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()
    rules_repo = FakeMappingRuleRepository()
    mb_client = FakeMbClient()

    canonical_mbid = "mbid-metallica"
    artist = _setup_resolved_artist(artist_repo, match_repo, canonical_mbid)

    # Score 71.43 for "enter sandman" vs "Sandman Returns" — above mid-band
    # (>64) but below strong_match_threshold (<80) → LOW_CONFIDENCE / NEEDS_REVIEW.
    lib_file = _lib_file(
        "/music/metallica/sandman_returns.flac",
        artist_mbid=canonical_mbid,
        track_title="Sandman Returns",
    )
    lib_repo.upsert(lib_file)

    identity = _pending_identity(
        artist_id=artist.id,
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    playlist_id = uuid4()
    _register_pending_for_playlist(identity_repo, identity, playlist_id)

    work_ids = match_identities_for_playlist(
        playlist_id=playlist_id,
        track_identity_repo=identity_repo,
        broadcast_artist_repo=artist_repo,
        match_repo=match_repo,
        library_file_repo=lib_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
        catalog_repo=FakeArtistRepository(),
    )

    assert work_ids == []
    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW

    # NEW: reason_code and reason_detail are now persisted.
    assert stored.reason_code is not None
    assert stored.reason_detail is not None


def test_match_identities_no_pending_returns_empty() -> None:
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()
    rules_repo = FakeMappingRuleRepository()
    mb_client = FakeMbClient()

    work_ids = match_identities_for_playlist(
        playlist_id=uuid4(),
        track_identity_repo=identity_repo,
        broadcast_artist_repo=artist_repo,
        match_repo=match_repo,
        library_file_repo=lib_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
        catalog_repo=FakeArtistRepository(),
    )
    assert work_ids == []


def test_match_identities_skips_orphaned_identity_without_artist() -> None:
    """Identity whose broadcast_artist_id has no BroadcastArtist row.

    Persists a terminal NEEDS_REVIEW with ORPHANED_IDENTITY reason so the
    orphan surfaces in review tooling instead of being reprocessed forever.
    No match row is created (no library_file candidate exists).
    """
    artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()
    rules_repo = FakeMappingRuleRepository()
    mb_client = FakeMbClient()

    identity = _pending_identity(
        artist_id=uuid4(),  # No BroadcastArtist row for this id.
        title="Enter Sandman",
        artist_normalized_name=normalize_artist("Metallica"),
    )
    playlist_id = uuid4()
    _register_pending_for_playlist(identity_repo, identity, playlist_id)

    work_ids = match_identities_for_playlist(
        playlist_id=playlist_id,
        track_identity_repo=identity_repo,
        broadcast_artist_repo=artist_repo,
        match_repo=match_repo,
        library_file_repo=lib_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
        catalog_repo=FakeArtistRepository(),
    )
    assert work_ids == []
    stored = identity_repo.get_by_id(identity.id)
    assert stored is not None
    # Orphan is surfaced as NEEDS_REVIEW so review tooling can see it.
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.match_tier == MatchTier.UNCLASSIFIED
    assert stored.reason_code is not None
    assert match_repo.get_by_identity(identity.id) is None


def test_broadcast_track_identity_dataclass_carries_reason_code_after_update() -> None:
    """Reason state is on the dataclass, not a sidecar."""
    from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository

    repo = FakeBroadcastTrackIdentityRepository()
    identity = BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=uuid4(),
        original_title="X",
        normalized_title="x",
        normalized_signature="x",
    )
    repo.upsert(identity)

    repo.update_match_status(
        identity.id,
        MatchStatus.NEEDS_REVIEW,
        MatchTier.UNCLASSIFIED,
        reason_code=ReasonCode.ORPHANED_IDENTITY,
        reason_detail="No BroadcastArtist row for this identity",
    )

    stored = repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.reason_code == ReasonCode.ORPHANED_IDENTITY
    assert stored.reason_detail == "No BroadcastArtist row for this identity"


def test_bulk_defer_by_artist_returns_count_and_only_touches_pending() -> None:
    from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository

    repo = FakeBroadcastTrackIdentityRepository()
    target = uuid4()
    other = uuid4()

    pending_target = BroadcastTrackIdentity(
        id=uuid4(), broadcast_artist_id=target,
        original_title="A", normalized_title="a", normalized_signature="sig-a",
    )
    matched_target = BroadcastTrackIdentity(
        id=uuid4(), broadcast_artist_id=target,
        original_title="B", normalized_title="b", normalized_signature="sig-b",
    )
    pending_other = BroadcastTrackIdentity(
        id=uuid4(), broadcast_artist_id=other,
        original_title="C", normalized_title="c", normalized_signature="sig-c",
    )
    repo.upsert(pending_target)
    repo.upsert(matched_target)
    repo.upsert(pending_other)
    repo.update_match_status(matched_target.id, MatchStatus.AUTO_MATCHED, MatchTier.NORMALIZATION)

    rows_deferred = repo.bulk_defer_by_artist(target)

    assert rows_deferred == 1
    after = repo.get_by_id(pending_target.id)
    assert after is not None
    assert after.match_status == MatchStatus.NEEDS_REVIEW
    assert after.reason_code == ReasonCode.DEFERRED_RETRY
    matched_after = repo.get_by_id(matched_target.id)
    assert matched_after is not None
    assert matched_after.match_status == MatchStatus.AUTO_MATCHED  # unchanged
    other_after = repo.get_by_id(pending_other.id)
    assert other_after is not None
    assert other_after.match_status == MatchStatus.PENDING  # unchanged


def test_reset_deferred_by_artist_ids_promotes_only_under_those_artists() -> None:
    from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository

    repo = FakeBroadcastTrackIdentityRepository()
    artist_a = uuid4()
    artist_b = uuid4()

    a = BroadcastTrackIdentity(
        id=uuid4(), broadcast_artist_id=artist_a,
        original_title="A", normalized_title="a", normalized_signature="sig-1",
    )
    b = BroadcastTrackIdentity(
        id=uuid4(), broadcast_artist_id=artist_b,
        original_title="B", normalized_title="b", normalized_signature="sig-2",
    )
    rejected = BroadcastTrackIdentity(
        id=uuid4(), broadcast_artist_id=artist_a,
        original_title="C", normalized_title="c", normalized_signature="sig-3",
    )
    repo.upsert(a)
    repo.upsert(b)
    repo.upsert(rejected)
    repo.bulk_defer_by_artist(artist_a)
    repo.bulk_defer_by_artist(artist_b)
    # Force one to AUTO_REJECTED to verify the reset is reason-code-scoped.
    repo.update_match_status(rejected.id, MatchStatus.AUTO_REJECTED, MatchTier.UNCLASSIFIED)

    rows_reset = repo.reset_deferred_by_artist_ids([artist_a])

    assert rows_reset == 1
    a_after = repo.get_by_id(a.id)
    b_after = repo.get_by_id(b.id)
    rejected_after = repo.get_by_id(rejected.id)
    assert a_after is not None and a_after.match_status == MatchStatus.PENDING
    assert b_after is not None and b_after.match_status == MatchStatus.NEEDS_REVIEW
    assert rejected_after is not None and rejected_after.match_status == MatchStatus.AUTO_REJECTED


def test_reset_deferred_by_artist_ids_empty_input_returns_zero() -> None:
    from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository

    repo = FakeBroadcastTrackIdentityRepository()
    assert repo.reset_deferred_by_artist_ids([]) == 0
