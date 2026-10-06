"""Acceptance tests: song matching honours recorded rejections (spec 2026-10-05 §4.1, AUD-R022).

D3: rejecting a suggestion rules out that (song, work) pair. The matcher skips each rejected file
and every candidate CURRENTLY in the same work as a rejected file. That holds at every candidate
source (Step A artist MBID, Step B MB recording search, Step C name fallback, Tier 2 fuzzy) and for
mapping rules. The runner-up stays eligible, and the work stays open for other songs.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, ReasonCode, TargetType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import NO_EXCLUSIONS, CandidateExclusions, MappingRule, Match
from backend.services.identity_matching_service import (
    BroadcastToLocalStrategy,
    IdentityMappingRuleStrategy,
    IdentityMatchingRepos,
    ResolvedArtistMbidStrategy,
    match_identities_for_playlist,
)
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from backend.services.title_scoring import broadcast_title_variants
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient

ARTIST = "Metallica"
NORM = normalize_artist(ARTIST)
MBID = "mbid-metallica"
TITLE = "Enter Sandman"


def _file(
    title: str,
    work_id: str | None,
    *,
    artist_mbid: str | None = None,
    recording_mbid: str | None = None,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4().hex}.flac",
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        work_id=work_id,
        audio=AudioMetadata(
            track_title=title,
            normalized_title=normalize_title(title),
            artist_name=ARTIST,
            normalized_artist_name=NORM,
            artist_mbid=artist_mbid,
            recording_mbid=recording_mbid,
        ),
    )


def _artist(status: MatchStatus = MatchStatus.PENDING) -> BroadcastArtist:
    return BroadcastArtist(
        id=uuid4(), original_name=ARTIST, normalized_name=NORM, match_status=status
    )


def _song(
    artist: BroadcastArtist, title: str = TITLE, rejected: tuple[UUID, ...] = ()
) -> BroadcastTrackIdentity:
    norm_title = normalize_title(title)
    return BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=artist.id,
        original_title=title,
        normalized_title=norm_title,
        normalized_signature=compute_normalized_signature(NORM, norm_title),
        rejected_file_ids=rejected,
    )


def _files(*files: LibraryFile) -> FakeLibraryFileRepository:
    repo = FakeLibraryFileRepository()
    for f in files:
        repo.upsert(f)
    return repo


def _rejecting(
    song: BroadcastTrackIdentity, *files: LibraryFile
) -> dict[UUID, CandidateExclusions]:
    return {
        song.id: CandidateExclusions(
            file_ids=frozenset(f.id for f in files),
            work_ids=frozenset(f.work_id for f in files if f.work_id),
        )
    }


# --- CandidateExclusions -------------------------------------------------------------------


def test_no_exclusions_allows_everything() -> None:
    assert NO_EXCLUSIONS.allows(_file(TITLE, "w1"))
    assert NO_EXCLUSIONS.allows(_file(TITLE, None))


def test_a_rejected_file_is_not_allowed() -> None:
    rejected = _file(TITLE, None)
    assert not CandidateExclusions(file_ids=frozenset({rejected.id})).allows(rejected)


def test_a_file_in_a_rejected_work_is_not_allowed() -> None:
    sibling = _file(TITLE, "w1")
    assert not CandidateExclusions(work_ids=frozenset({"w1"})).allows(sibling)


def test_a_file_in_another_work_or_without_a_work_is_allowed() -> None:
    exclusions = CandidateExclusions(file_ids=frozenset({uuid4()}), work_ids=frozenset({"w1"}))
    assert exclusions.allows(_file(TITLE, "w2"))
    assert exclusions.allows(_file(TITLE, None))
    assert exclusions.allows(_file(TITLE, ""))


# --- Tier 2 fuzzy (BroadcastToLocalStrategy) ---------------------------------------------------


def test_tier2_skips_the_rejected_work_and_picks_the_runner_up() -> None:
    artist = _artist()
    best, sibling, runner = _file(TITLE, "w1"), _file(TITLE, "w1"), _file(f"{TITLE} (Live)", "w2")
    song = _song(artist, rejected=(best.id,))
    strategy = BroadcastToLocalStrategy(
        _files(best, sibling, runner), exclusions_by_identity=_rejecting(song, best)
    )

    result = strategy.apply(song, artist)

    assert result is not None
    assert result.library_file_id == runner.id


def test_tier2_without_rejections_still_picks_the_best_work() -> None:
    artist = _artist()
    best, sibling, runner = _file(TITLE, "w1"), _file(TITLE, "w1"), _file(f"{TITLE} (Live)", "w2")
    song = _song(artist)

    result = BroadcastToLocalStrategy(_files(best, sibling, runner)).apply(song, artist)

    assert result is not None
    assert result.library_file_id in {best.id, sibling.id}


def test_tier2_all_candidates_rejected_goes_to_review_without_suggestion() -> None:
    artist = _artist()
    only = _file(TITLE, "w1")
    song = _song(artist, rejected=(only.id,))
    strategy = BroadcastToLocalStrategy(_files(only), exclusions_by_identity=_rejecting(song, only))

    result = strategy.apply(song, artist)

    assert result is not None
    assert result.status == MatchStatus.NEEDS_REVIEW
    assert result.library_file_id is None
    assert result.reason_code == ReasonCode.NO_CANDIDATES


def test_another_song_may_still_get_the_rejected_work() -> None:
    artist = _artist()
    best, runner = _file(TITLE, "w1"), _file(f"{TITLE} (Live)", "w2")
    rejecting_song = _song(artist, rejected=(best.id,))
    other_song = _song(artist)
    strategy = BroadcastToLocalStrategy(
        _files(best, runner), exclusions_by_identity=_rejecting(rejecting_song, best)
    )

    result = strategy.apply(other_song, artist)

    assert result is not None
    assert result.library_file_id == best.id


# --- ResolvedArtistMbidStrategy: Step A, Step B, Step C --------------------------------------


def _resolved_artist(match_repo: FakeMatchRepository) -> BroadcastArtist:
    artist = _artist(MatchStatus.AUTO_MATCHED)
    match_repo.create(
        Match(
            id=uuid4(),
            artist_id=artist.id,
            target_id=MBID,
            target_type=TargetType.ARTIST,
            confidence_score=100.0,
            match_tier=MatchTier.MUSICBRAINZ_ID_EXACT,
        )
    )
    return artist


def test_step_a_skips_the_rejected_work() -> None:
    matches = FakeMatchRepository()
    artist = _resolved_artist(matches)
    best = _file(TITLE, "w1", artist_mbid=MBID)
    sibling = _file(TITLE, "w1", artist_mbid=MBID)
    runner = _file(f"{TITLE} (Live)", "w2", artist_mbid=MBID)
    song = _song(artist, rejected=(best.id,))
    strategy = ResolvedArtistMbidStrategy(
        _files(best, sibling, runner),
        matches,
        FakeMbClient(),
        FakeArtistRepository(),
        80,
        exclusions_by_identity=_rejecting(song, best),
    )

    result = strategy.apply(song, artist)

    assert result is not None
    assert result.library_file_id == runner.id


def test_step_b_skips_the_rejected_work() -> None:
    matches = FakeMatchRepository()
    artist = _resolved_artist(matches)
    rejected = _file(TITLE, "w1", recording_mbid="rec-1")
    sibling = _file(TITLE, "w1", recording_mbid="rec-1")
    allowed = _file(f"{TITLE} (Live)", "w2", recording_mbid="rec-1")
    song = _song(artist, rejected=(rejected.id,))
    mb = FakeMbClient(
        recording_searches={(MBID, broadcast_title_variants(TITLE)[0]): [{"id": "rec-1"}]}
    )
    strategy = ResolvedArtistMbidStrategy(
        _files(rejected, sibling, allowed),
        matches,
        mb,
        FakeArtistRepository(),
        80,
        exclusions_by_identity=_rejecting(song, rejected),
    )

    result = strategy.apply(song, artist)

    assert result is not None
    assert result.library_file_id == allowed.id
    assert result.tier == MatchTier.MUSICBRAINZ_ID_SEARCH


def test_step_c_skips_the_rejected_work() -> None:
    matches = FakeMatchRepository()
    artist = _resolved_artist(matches)
    best = _file(TITLE, "w1")
    sibling = _file(TITLE, "w1")
    runner = _file(f"{TITLE} (Live)", "w2")
    song = _song(artist, rejected=(best.id,))
    strategy = ResolvedArtistMbidStrategy(
        _files(best, sibling, runner),
        matches,
        FakeMbClient(),
        FakeArtistRepository(),
        80,
        exclusions_by_identity=_rejecting(song, best),
    )

    result = strategy.apply(song, artist)

    assert result is not None
    assert result.library_file_id == runner.id
    assert result.tier == MatchTier.LOCAL_FILE_FUZZY


# --- Mapping rules -----------------------------------------------------------------------------


def _rule(song: BroadcastTrackIdentity, target: LibraryFile, priority: int) -> MappingRule:
    return MappingRule(
        id=uuid4(),
        source_pattern=song.normalized_signature,
        target_type=TargetType.LIBRARY_FILE,
        target_id=target.file_path,
        priority=priority,
    )


def test_a_rule_pointing_at_a_rejected_work_is_skipped_for_the_next_rule() -> None:
    artist = _artist()
    rejected = _file(TITLE, "w1")
    sibling = _file(TITLE, "w1")
    allowed = _file(TITLE, "w2")
    song = _song(artist, rejected=(rejected.id,))
    strategy = IdentityMappingRuleStrategy(
        [_rule(song, rejected, 20), _rule(song, sibling, 15), _rule(song, allowed, 10)],
        _files(rejected, sibling, allowed),
        exclusions_by_identity=_rejecting(song, rejected),
    )

    result = strategy.apply(song, artist)

    assert result is not None
    assert result.library_file_id == allowed.id


def test_a_rule_whose_only_target_is_rejected_falls_through() -> None:
    artist = _artist()
    rejected = _file(TITLE, "w1")
    song = _song(artist, rejected=(rejected.id,))
    strategy = IdentityMappingRuleStrategy(
        [_rule(song, rejected, 10)],
        _files(rejected),
        exclusions_by_identity=_rejecting(song, rejected),
    )

    assert strategy.apply(song, artist) is None


# --- Orchestration: rejections are loaded once, by CURRENT work ------------------------------


class _CountingFiles(FakeLibraryFileRepository):
    def __init__(self) -> None:
        super().__init__()
        self.batch_calls: list[list[UUID]] = []

    def get_by_ids(self, ids: list[UUID]) -> list[LibraryFile]:
        self.batch_calls.append(list(ids))
        return super().get_by_ids(ids)


def _run(
    song: BroadcastTrackIdentity,
    artist: BroadcastArtist,
    files: FakeLibraryFileRepository,
    *,
    matches: FakeMatchRepository | None = None,
    rules: list[MappingRule] | None = None,
) -> FakeMatchRepository:
    playlist_id = uuid4()
    artists = FakeBroadcastArtistRepository()
    artists.upsert(artist)
    identities = FakeBroadcastTrackIdentityRepository()
    identities.upsert(song)
    identities.register_playlist_identity(playlist_id, song.id)
    if matches is None:
        matches = FakeMatchRepository()
    rules_repo = FakeMappingRuleRepository()
    if rules:
        for rule in rules:
            rules_repo.create(rule)
    match_identities_for_playlist(
        playlist_id=playlist_id,
        repos=IdentityMatchingRepos(
            track_identity_repo=identities,
            broadcast_artist_repo=artists,
            match_repo=matches,
            library_file_repo=files,
            rules_repo=rules_repo,
            catalog_repo=FakeArtistRepository(),
        ),
        mb_client=FakeMbClient(),
    )
    return matches


def test_exclusion_follows_the_rejected_files_current_work() -> None:
    """The rejected file now sits in work w3 (merged or moved since the reject): w3 is excluded,
    and the work it used to be in is not."""
    artist = _artist()
    rejected = _file(TITLE, "w3")
    same_work_now = _file(TITLE, "w3")
    old_work = _file(f"{TITLE} (Live)", "w1")
    files = _CountingFiles()
    for f in (rejected, same_work_now, old_work):
        files.upsert(f)
    song = _song(artist, rejected=(rejected.id,))

    matches = _run(song, artist, files)

    created = matches.get_by_identity(song.id)
    assert created is not None
    assert created.library_file_id == old_work.id
    assert files.batch_calls == [[rejected.id]]


def test_no_rejections_means_no_batch_lookup() -> None:
    artist = _artist()
    files = _CountingFiles()
    files.upsert(_file(TITLE, "w1"))

    _run(_song(artist), artist, files)

    assert files.batch_calls == []


def test_purged_rejected_ids_do_not_break_matching() -> None:
    artist = _artist()
    present = _file(TITLE, "w1")
    files = _CountingFiles()
    files.upsert(present)
    song = _song(artist, rejected=(uuid4(),))

    matches = _run(song, artist, files)

    created = matches.get_by_identity(song.id)
    assert created is not None
    assert created.library_file_id == present.id


@pytest.mark.parametrize("work_id", ["w1", None])
def test_the_rejected_file_itself_is_never_suggested(work_id: str | None) -> None:
    artist = _artist()
    rejected = _file(TITLE, work_id)
    files = _CountingFiles()
    files.upsert(rejected)
    song = _song(artist, rejected=(rejected.id,))

    matches = _run(song, artist, files)

    created = matches.get_by_identity(song.id)
    assert created is None or created.library_file_id != rejected.id


def test_orchestrated_resolved_artist_with_rejections() -> None:
    """Full match_identities_for_playlist flow: resolved artist, rejected work excluded."""
    artist_matches = FakeMatchRepository()
    artist = _resolved_artist(artist_matches)
    best = _file(TITLE, "w1", artist_mbid=MBID)
    runner = _file(f"{TITLE} (Live)", "w2", artist_mbid=MBID)
    files = FakeLibraryFileRepository()
    files.upsert(best)
    files.upsert(runner)
    song = _song(artist, rejected=(best.id,))

    matches = _run(song, artist, files, matches=artist_matches)

    created = matches.get_by_identity(song.id)
    assert created is not None
    assert created.library_file_id == runner.id


def test_orchestrated_mapping_rules_with_rejections() -> None:
    """Full match_identities_for_playlist flow: rules, rejected work excluded."""
    artist = _artist()
    rejected = _file(TITLE, "w1")
    allowed = _file(TITLE, "w2")
    files = FakeLibraryFileRepository()
    files.upsert(rejected)
    files.upsert(allowed)
    song = _song(artist, rejected=(rejected.id,))
    rule_rejected = _rule(song, rejected, 20)
    rule_allowed = _rule(song, allowed, 10)

    matches = _run(song, artist, files, rules=[rule_rejected, rule_allowed])

    created = matches.get_by_identity(song.id)
    assert created is not None
    assert created.library_file_id == allowed.id
