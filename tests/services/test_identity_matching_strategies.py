"""Tests for the three IdentityMatchingStrategy implementations.

Strategies produce IdentityMatchResult values; they do not persist. The
orchestrator (Task 10) wires them together. These tests exercise each
strategy in isolation using in-memory fakes.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.catalog import Artist
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
from backend.services.normalization import normalize_title
from tests.fakes.artists import FakeArtistRepository
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


_UNSET: object = object()


def _lib_file(
    path: str,
    *,
    track_title: str | None = None,
    normalized_title: str | None = None,
    artist_mbid: str | None = None,
    artist_name: str | None = None,
    normalized_artist_name: str | None | object = _UNSET,
    recording_mbid: str | None = None,
    work_id: str | None = None,
    recording_id: str | None = None,
) -> LibraryFile:
    """Build a LibraryFile fixture matching how library_scan_service populates rows.

    When normalized_title is omitted, default-compute it from track_title via
    normalize_title() — exactly what the scanner does at ingest.

    When normalized_artist_name is omitted, default to artist_name.lower() so
    fixtures interop with the locked-artist guard (_filter_to_artist) without
    every test having to spell it out. Pass an explicit None to exercise the
    fail-closed null path.
    """
    if normalized_title is None and track_title is not None:
        normalized_title = normalize_title(track_title)
    if normalized_artist_name is _UNSET:
        norm_artist: str | None = (
            artist_name.lower() if artist_name is not None else None
        )
    else:
        norm_artist = normalized_artist_name  # type: ignore[assignment]
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
            normalized_title=normalized_title,
            artist_mbid=artist_mbid,
            artist_name=artist_name,
            normalized_artist_name=norm_artist,
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
    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb, FakeArtistRepository())

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
        "/m/es.flac", track_title="enter sandman", artist_mbid="mbid-m",
        artist_name="metallica", work_id="w-es",
    ))

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb, FakeArtistRepository())
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
        artist_name="metallica",
    ))
    # MB returns recording -> that recording maps to a DIFFERENT local file.
    # Note: hit has NO artist_mbid so Step A does not find it; only Step B does.
    hit = _lib_file(
        "/m/real.flac",
        track_title="enter sandman",
        recording_mbid="rec-abc",
        artist_name="metallica",
    )
    lib_repo.upsert(hit)
    mb = FakeMbClient(
        recording_searches={("mbid-m", "enter sandman"): [{"id": "rec-abc"}]},
    )

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb, FakeArtistRepository())
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
        artist_name="metallica",
    )
    lib_repo.upsert(hit)
    mb = FakeMbClient(
        recording_searches={("mbid-m", "enter sandman"): [{"id": "rec-abc"}]},
    )

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb, FakeArtistRepository())
    identity = _identity(artist.id, title="enter sandman")
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.MUSICBRAINZ_ID_SEARCH
    assert result.library_file_id == hit.id


def test_tier1_step_b_tie_keeps_step_a_result() -> None:
    """Step B escalation must only displace Step A on STRICT score
    improvement. When both find candidates that score equally, Step A wins —
    its MUSICBRAINZ_ID_EXACT tier is the more trustworthy label, and its
    candidate is anchored on the resolved artist MBID rather than a
    downstream MB recording-search hit (which can pull in cross-artist files
    when a local tag carries a foreign recording_mbid).
    """
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    artist = _artist()
    _seed_artist_match(match_repo, artist.id, "mbid-m")

    # Step A candidate — shared partial-match title scores below
    # MB_AUTO_LINK_SCORE (95), forcing Step B escalation.
    step_a = _lib_file(
        "/m/step_a.flac",
        track_title="enter completely unrelated extras",
        artist_mbid="mbid-m",
        artist_name="metallica",
    )
    # Step B candidate — same title (so identical score) but reachable only
    # via recording_mbid path; Step A does not see it because it lacks
    # artist_mbid="mbid-m".
    step_b = _lib_file(
        "/m/step_b.flac",
        track_title="enter completely unrelated extras",
        recording_mbid="rec-abc",
        artist_name="metallica",
    )
    lib_repo.upsert(step_a)
    lib_repo.upsert(step_b)
    mb = FakeMbClient(
        recording_searches={("mbid-m", "enter sandman"): [{"id": "rec-abc"}]},
    )

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb, FakeArtistRepository())
    identity = _identity(artist.id, title="enter sandman")
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW  # mid-band shared score
    assert result.tier == MatchTier.MUSICBRAINZ_ID_EXACT  # NOT _SEARCH
    assert result.library_file_id == step_a.id  # NOT step_b.id


def test_tier1_step_b_strictly_better_score_wins() -> None:
    """Conversely: when Step B genuinely improves on Step A, Step B wins
    and the result tier flips to MUSICBRAINZ_ID_SEARCH. Guards against
    a too-aggressive interpretation of the change-2 tie rule.
    """
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    artist = _artist()
    _seed_artist_match(match_repo, artist.id, "mbid-m")

    # Step A: partial match (mid-band score), forces Step B.
    step_a = _lib_file(
        "/m/step_a.flac",
        track_title="enter unrelated junk extras here",
        artist_mbid="mbid-m",
        artist_name="metallica",
    )
    # Step B: exact title, scores 100 — strictly better than Step A.
    step_b = _lib_file(
        "/m/step_b.flac",
        track_title="enter sandman",
        recording_mbid="rec-abc",
        artist_name="metallica",
    )
    lib_repo.upsert(step_a)
    lib_repo.upsert(step_b)
    mb = FakeMbClient(
        recording_searches={("mbid-m", "enter sandman"): [{"id": "rec-abc"}]},
    )

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb, FakeArtistRepository())
    identity = _identity(artist.id, title="enter sandman")
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.MUSICBRAINZ_ID_SEARCH
    assert result.library_file_id == step_b.id


def test_tier1_mb_inconclusive_returns_no_local_files_reason() -> None:
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    artist = _artist()
    _seed_artist_match(match_repo, artist.id, "mbid-m")
    mb = FakeMbClient()  # no recording_searches => empty list

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb, FakeArtistRepository())
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

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb, FakeArtistRepository())
    artist = _artist(status=MatchStatus.AUTO_MATCHED)
    identity = _identity(artist.id)
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.reason_code == ReasonCode.MISSING_MATCH_RECORD


def test_tier1_local_only_target_id_no_mbid_skips_mb_uses_name_search() -> None:
    """When target_id is a local catalog UUID for a true local-only artist
    (catalog.mbid is None), real_mbid resolves to None, so Steps A and B are
    skipped entirely. Step C (name-based search) must AUTO_MATCH when a file
    with a matching artist_name exists.
    """
    local_artist_uuid = str(uuid4())

    catalog_repo = FakeArtistRepository()
    catalog_repo.upsert(Artist(
        id=local_artist_uuid,
        name="Van Halen",
        sort_name="Van Halen",
        mbid=None,  # local-only: no MusicBrainz ID yet
        normalized_name="van halen",
    ))

    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    artist = _artist(name="van halen")
    match_repo.create(Match(
        id=uuid4(),
        artist_id=artist.id,
        confidence_score=100.0,
        match_tier=MatchTier.NORMALIZATION,
        target_id=local_artist_uuid,
        target_type=TargetType.ARTIST,
    ))
    hit = _lib_file("/music/vh/jump.flac", track_title="jump", artist_name="van halen")
    lib_repo.upsert(hit)
    mb = FakeMbClient()  # must NOT be called

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb, catalog_repo)
    result = strategy.apply(_identity(artist.id, title="jump"), artist)

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.LOCAL_FILE_FUZZY
    assert result.library_file_id == hit.id
    assert mb.calls == []  # Steps A and B must not fire


def test_tier1_local_uuid_with_real_mbid_uses_mbid_for_steps_a_b() -> None:
    """When target_id is a local catalog UUID but catalog_artist.mbid is set,
    real_mbid resolves to that MBID and Steps A/B fire correctly.

    This covers the case where a local-only artist later gets linked to MB
    but the match record still stores the local catalog UUID as target_id.
    """
    local_artist_uuid = str(uuid4())
    artist_mbid = "mbid-vh-real"

    catalog_repo = FakeArtistRepository()
    catalog_repo.upsert(Artist(
        id=local_artist_uuid,
        name="Van Halen",
        sort_name="Van Halen",
        mbid=artist_mbid,  # now linked to MusicBrainz
        normalized_name="van halen",
    ))

    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    artist = _artist(name="van halen")
    match_repo.create(Match(
        id=uuid4(),
        artist_id=artist.id,
        confidence_score=100.0,
        match_tier=MatchTier.NORMALIZATION,
        target_id=local_artist_uuid,  # still the local UUID from initial match
        target_type=TargetType.ARTIST,
    ))
    # Library file linked via the real MBID (Step A path).
    hit = _lib_file(
        "/music/vh/jump.flac",
        track_title="jump",
        artist_mbid=artist_mbid,
        artist_name="van halen",
    )
    lib_repo.upsert(hit)
    mb = FakeMbClient()

    strategy = ResolvedArtistMbidStrategy(lib_repo, match_repo, mb, catalog_repo)
    result = strategy.apply(_identity(artist.id, title="jump"), artist)

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.MUSICBRAINZ_ID_EXACT
    assert result.library_file_id == hit.id


def test_tier1_step_c_passes_normalized_name_not_original() -> None:
    """Step C calls get_by_normalized_artist_name with artist.normalized_name,
    not original_name. The repo enforces equality on normalized_artist_name —
    both sides come out of normalize_artist() — so passing original_name
    (e.g. "AC/DC") would never match a library file whose normalized form is
    "ac dc". The previous original_name + LIKE-substring path silently
    accepted cross-artist files; this test pins the new contract.
    """
    local_artist_uuid = str(uuid4())
    catalog_repo = FakeArtistRepository()
    catalog_repo.upsert(Artist(
        id=local_artist_uuid,
        name="Van Halen",
        sort_name="Van Halen",
        mbid=None,
        normalized_name="van halen",
    ))

    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    artist = BroadcastArtist(
        id=uuid4(),
        original_name="Van Halen",
        normalized_name="van halen",
        match_status=MatchStatus.AUTO_MATCHED,
    )
    match_repo.create(Match(
        id=uuid4(),
        artist_id=artist.id,
        confidence_score=100.0,
        match_tier=MatchTier.NORMALIZATION,
        target_id=local_artist_uuid,
        target_type=TargetType.ARTIST,
    ))
    hit = _lib_file(
        "/music/vh/jump.flac",
        track_title="jump",
        artist_name="Van Halen",
        normalized_artist_name="van halen",
    )
    lib_repo.upsert(hit)

    strategy = ResolvedArtistMbidStrategy(
        lib_repo, match_repo, FakeMbClient(), catalog_repo,
    )
    result = strategy.apply(_identity(artist.id, title="jump"), artist)

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
    assert result.tier == MatchTier.LOCAL_FILE_FUZZY
    assert result.library_file_id == hit.id


def test_tier1_step_c_never_proposes_cross_artist_file() -> None:
    """Locked-artist invariant for Step C — the original Hendrix/U2 bug.

    Artist is "Jimi Hendrix" (resolved, no MBID-tagged local files). Library
    contains a U2 "The Star Spangled Banner" plus a Hendrix "Star Spangled
    Banner". Step C must pick the Hendrix file — never the U2 file — even
    though token_sort_ratio would score the U2 title competitively.
    """
    local_artist_uuid = str(uuid4())
    catalog_repo = FakeArtistRepository()
    catalog_repo.upsert(Artist(
        id=local_artist_uuid,
        name="Jimi Hendrix",
        sort_name="Hendrix, Jimi",
        mbid=None,
        normalized_name="jimi hendrix",
    ))

    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    artist = _artist(name="jimi hendrix")
    match_repo.create(Match(
        id=uuid4(),
        artist_id=artist.id,
        confidence_score=100.0,
        match_tier=MatchTier.NORMALIZATION,
        target_id=local_artist_uuid,
        target_type=TargetType.ARTIST,
    ))
    # Wrong-artist seed — must never be proposed.
    lib_repo.upsert(_lib_file(
        "/music/u2/star_spangled_banner.flac",
        track_title="The Star Spangled Banner",
        artist_name="U2",
        normalized_artist_name="u2",
    ))
    # Right-artist seed — must win.
    hendrix = _lib_file(
        "/music/hendrix/star_spangled_banner.flac",
        track_title="Star Spangled Banner",
        artist_name="Jimi Hendrix",
        normalized_artist_name="jimi hendrix",
    )
    lib_repo.upsert(hendrix)

    strategy = ResolvedArtistMbidStrategy(
        lib_repo, match_repo, FakeMbClient(), catalog_repo,
    )
    result = strategy.apply(
        _identity(artist.id, title="star spangled banner"), artist,
    )

    assert result is not None
    assert result.library_file_id == hendrix.id


def test_tier1_step_c_no_same_artist_files_returns_no_local_files() -> None:
    """If the only files in the library are by a different artist, Step C
    must return NO_LOCAL_FILES — never silently propose the wrong-artist file
    even when its title scores high.
    """
    local_artist_uuid = str(uuid4())
    catalog_repo = FakeArtistRepository()
    catalog_repo.upsert(Artist(
        id=local_artist_uuid,
        name="Jimi Hendrix",
        sort_name="Hendrix, Jimi",
        mbid=None,
        normalized_name="jimi hendrix",
    ))

    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    artist = _artist(name="jimi hendrix")
    match_repo.create(Match(
        id=uuid4(),
        artist_id=artist.id,
        confidence_score=100.0,
        match_tier=MatchTier.NORMALIZATION,
        target_id=local_artist_uuid,
        target_type=TargetType.ARTIST,
    ))
    lib_repo.upsert(_lib_file(
        "/music/u2/star_spangled_banner.flac",
        track_title="The Star Spangled Banner",
        artist_name="U2",
        normalized_artist_name="u2",
    ))

    strategy = ResolvedArtistMbidStrategy(
        lib_repo, match_repo, FakeMbClient(), catalog_repo,
    )
    result = strategy.apply(
        _identity(artist.id, title="star spangled banner"), artist,
    )

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.reason_code == ReasonCode.NO_LOCAL_FILES
    assert result.library_file_id is None


def test_tier1_step_b_filters_cross_artist_tagged_recording() -> None:
    """Lines 343-347 of identity_matching_service warn that local tags
    carrying someone else's recording_mbid can pull cross-artist files into
    Step B. _filter_to_artist now enforces that as a hard guard. With only a
    cross-artist file mapped via MB recording search, Step B returns None
    (no candidates after filtering), and the strategy falls through to the
    NO_LOCAL_FILES result.
    """
    lib_repo = FakeLibraryFileRepository()
    match_repo = FakeMatchRepository()
    artist = _artist(name="metallica")
    _seed_artist_match(match_repo, artist.id, "mbid-m")
    # File mistagged with a Metallica recording_mbid but artist is U2.
    lib_repo.upsert(_lib_file(
        "/m/wrong_artist.flac",
        track_title="enter sandman",
        artist_name="U2",
        normalized_artist_name="u2",
        recording_mbid="rec-abc",
    ))
    mb = FakeMbClient(
        recording_searches={("mbid-m", "enter sandman"): [{"id": "rec-abc"}]},
    )

    strategy = ResolvedArtistMbidStrategy(
        lib_repo, match_repo, mb, FakeArtistRepository(),
    )
    result = strategy.apply(
        _identity(artist.id, title="enter sandman"), artist,
    )

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.reason_code == ReasonCode.NO_LOCAL_FILES
    assert result.library_file_id is None


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
    file scores above strong_match_threshold (80) but below MB_AUTO_LINK_SCORE (95).
    Without has_competitor on the strong_match_threshold+gap clause, synthesized
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

    strat = BroadcastToLocalStrategy(lib_repo, strong_match_threshold=80)
    result = strat.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    # Reason is LOW_CONFIDENCE, not AMBIGUOUS_GAP — no peer for ambiguity.
    assert result.reason_code == ReasonCode.LOW_CONFIDENCE


def test_tier2_filters_cross_artist_files() -> None:
    """Tier 2 (unresolved artist) must apply the locked-artist guard too.

    Library contains a U2 file titled "The Star Spangled Banner" plus a
    Jimi Hendrix file titled "Star Spangled Banner". A pending Hendrix
    artist must not surface the U2 file as a proposal.
    """
    lib_repo = FakeLibraryFileRepository()
    lib_repo.upsert(_lib_file(
        "/music/u2/ssb.flac",
        track_title="The Star Spangled Banner",
        artist_name="U2",
        normalized_artist_name="u2",
    ))
    hendrix = _lib_file(
        "/music/hendrix/ssb.flac",
        track_title="Star Spangled Banner",
        artist_name="Jimi Hendrix",
        normalized_artist_name="jimi hendrix",
    )
    lib_repo.upsert(hendrix)

    strategy = BroadcastToLocalStrategy(lib_repo)
    artist = _artist(name="jimi hendrix", status=MatchStatus.PENDING)
    identity = _identity(artist.id, title="star spangled banner")
    result = strategy.apply(identity, artist)

    assert result is not None
    assert result.library_file_id == hendrix.id


def test_filter_to_artist_drops_null_normalized_artist_name() -> None:
    """Fail-closed policy: a candidate with normalized_artist_name=None is
    dropped. Locks the rule so a future change cannot quietly let nulls
    through as wildcard matches.
    """
    from backend.services.identity_matching_service import _filter_to_artist

    null_norm = _lib_file(
        "/m/anon.flac",
        track_title="enter sandman",
        artist_name="Metallica",
        normalized_artist_name=None,
    )
    matching = _lib_file(
        "/m/m.flac",
        track_title="enter sandman",
        artist_name="Metallica",
        normalized_artist_name="metallica",
    )

    kept = _filter_to_artist([null_norm, matching], "metallica")
    assert kept == [matching]


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

    strat = BroadcastToLocalStrategy(lib_repo, strong_match_threshold=80)
    result = strat.apply(identity, artist)

    assert result is not None
    assert result.status == MatchStatus.AUTO_MATCHED
