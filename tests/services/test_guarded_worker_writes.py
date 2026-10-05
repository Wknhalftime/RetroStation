"""Acceptance tests for AUD-R018: a matching worker never overwrites a decision made meanwhile.

The race (finding AUD-060): a matching worker reads PENDING rows, spends seconds to minutes on
lookups, then writes its result. If the user decides an item in the review UI during that window
(the API runs in another process, so -w 1 does not serialise them), the worker's blind write
replaced the decision. Lance (2026-10-05) extended AUD-R018 from artists to identities.

Requirements:
- G1 (contract): `update_match_status_if_pending` writes only when the row is still PENDING and
  returns whether it wrote. A row in any other status is left exactly as it was.
- G2 (artists): when the worker's write is refused, nothing that follows from its own result
  happens: no match row, no catalog upsert, no DEFERRED_RETRY cascade to the artist's songs.
  Other artists in the same run are written as usual.
- G3 (identities): the same for all three identity write sites (normal result, orphaned
  identity, exhausted engine). No match row, and no work_id returned for master recalculation.

The race is simulated by a fake whose `get_pending_for_playlist` returns the PENDING snapshot and
then applies the user's decision to the store, exactly as an API write landing just after the
worker's read would. The same fake refuses the plain `update_match_status`, so re-reading the row
and then writing it blindly (still racy against another process) cannot pass these tests.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

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
from backend.domain.matching import MappingRule
from backend.services.artist_matching_service import (
    ArtistMatchingRepos,
    match_artists_for_playlist,
)
from backend.services.identity_matching_service import (
    IdentityMatchingRepos,
    IdentityMatchResult,
    match_identities_for_playlist,
)
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

# ---------------------------------------------------------------------------
# Race-simulating fakes
# ---------------------------------------------------------------------------


class _ArtistsDecidedAfterRead(FakeBroadcastArtistRepository):
    """The user decides some artists just after the worker has read them as PENDING."""

    def __init__(self) -> None:
        super().__init__()
        self.decide_after_read: dict[UUID, MatchStatus] = {}

    def get_pending_for_playlist(self, playlist_id: UUID) -> list[BroadcastArtist]:
        snapshot = super().get_pending_for_playlist(playlist_id)
        for artist_id, decision in self.decide_after_read.items():
            super().update_match_status(artist_id, decision)
        return snapshot

    def update_match_status(
        self,
        artist_id: UUID,
        status: MatchStatus,
        reason_code: ReasonCode | None = None,
        reason_detail: str | None = None,
    ) -> None:
        raise AssertionError("the worker must use update_match_status_if_pending (AUD-R018)")

    def update_match_status_if_pending(
        self,
        artist_id: UUID,
        status: MatchStatus,
        reason_code: ReasonCode | None = None,
        reason_detail: str | None = None,
    ) -> bool:
        current = self.get_by_id(artist_id)
        if current is None or current.match_status != MatchStatus.PENDING:
            return False
        super().update_match_status(artist_id, status, reason_code, reason_detail)
        return True


class _IdentitiesDecidedAfterRead(FakeBroadcastTrackIdentityRepository):
    """The user decides some identities just after the worker has read them as PENDING."""

    def __init__(self) -> None:
        super().__init__()
        self.decide_after_read: dict[UUID, MatchStatus] = {}

    def get_pending_for_playlist(self, playlist_id: UUID) -> list[BroadcastTrackIdentity]:
        snapshot = super().get_pending_for_playlist(playlist_id)
        for identity_id, decision in self.decide_after_read.items():
            super().update_match_status(identity_id, decision, MatchTier.MANUAL)
        return snapshot

    def update_match_status(
        self,
        identity_id: UUID,
        status: MatchStatus,
        tier: MatchTier | None,
        reason_code: ReasonCode | None = None,
        reason_detail: str | None = None,
    ) -> None:
        raise AssertionError("the worker must use update_match_status_if_pending (AUD-R018)")

    def update_match_status_if_pending(
        self,
        identity_id: UUID,
        status: MatchStatus,
        tier: MatchTier | None,
        reason_code: ReasonCode | None = None,
        reason_detail: str | None = None,
    ) -> bool:
        current = self.get_by_id(identity_id)
        if current is None or current.match_status != MatchStatus.PENDING:
            return False
        super().update_match_status(identity_id, status, tier, reason_code, reason_detail)
        return True


# ---------------------------------------------------------------------------
# G1: the guarded write's contract (fakes; Pg is covered in tests/integration)
# ---------------------------------------------------------------------------


def _artist(repo: FakeBroadcastArtistRepository, name: str = "METALLICA") -> BroadcastArtist:
    artist = BroadcastArtist(id=uuid4(), original_name=name, normalized_name=normalize_artist(name))
    return repo.upsert(artist)


def _identity(
    repo: FakeBroadcastTrackIdentityRepository, artist_id: UUID, title: str = "Enter Sandman"
) -> BroadcastTrackIdentity:
    norm_title = normalize_title(title)
    identity = BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=artist_id,
        original_title=title,
        normalized_title=norm_title,
        normalized_signature=compute_normalized_signature("metallica", norm_title),
    )
    return repo.upsert(identity)


def test_artist_guarded_write_applies_to_a_pending_row() -> None:
    repo = FakeBroadcastArtistRepository()
    artist = _artist(repo)

    wrote = repo.update_match_status_if_pending(
        artist.id,
        MatchStatus.NEEDS_REVIEW,
        reason_code=ReasonCode.DEFERRED_RETRY,
        reason_detail="retry later",
    )

    assert wrote is True
    stored = repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.reason_code == ReasonCode.DEFERRED_RETRY
    assert stored.reason_detail == "retry later"


@pytest.mark.parametrize(
    "decided",
    [MatchStatus.MANUAL_MATCHED, MatchStatus.MANUAL_REJECTED, MatchStatus.AUTO_MATCHED],
)
def test_artist_guarded_write_leaves_a_decided_row_untouched(decided: MatchStatus) -> None:
    repo = FakeBroadcastArtistRepository()
    artist = _artist(repo)
    repo.update_match_status(artist.id, decided, reason_detail="kept")

    wrote = repo.update_match_status_if_pending(
        artist.id, MatchStatus.AUTO_REJECTED, reason_code=ReasonCode.NO_CANDIDATES
    )

    assert wrote is False
    stored = repo.get_by_id(artist.id)
    assert stored is not None
    assert stored.match_status == decided
    assert stored.reason_code is None
    assert stored.reason_detail == "kept"


def test_identity_guarded_write_applies_to_a_pending_row() -> None:
    artists = FakeBroadcastArtistRepository()
    repo = FakeBroadcastTrackIdentityRepository()
    identity = _identity(repo, _artist(artists).id)

    wrote = repo.update_match_status_if_pending(
        identity.id,
        MatchStatus.AUTO_MATCHED,
        MatchTier.NORMALIZATION,
        reason_code=None,
        reason_detail=None,
    )

    assert wrote is True
    stored = repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert stored.match_tier == MatchTier.NORMALIZATION


@pytest.mark.parametrize(
    "decided",
    [MatchStatus.MANUAL_MATCHED, MatchStatus.MANUAL_REJECTED, MatchStatus.AUTO_REJECTED],
)
def test_identity_guarded_write_leaves_a_decided_row_untouched(decided: MatchStatus) -> None:
    artists = FakeBroadcastArtistRepository()
    repo = FakeBroadcastTrackIdentityRepository()
    identity = _identity(repo, _artist(artists).id)
    repo.update_match_status(identity.id, decided, MatchTier.MANUAL, reason_detail="kept")

    wrote = repo.update_match_status_if_pending(
        identity.id,
        MatchStatus.AUTO_MATCHED,
        MatchTier.NORMALIZATION,
        reason_code=ReasonCode.LOW_CONFIDENCE,
    )

    assert wrote is False
    stored = repo.get_by_id(identity.id)
    assert stored is not None
    assert stored.match_status == decided
    assert stored.match_tier == MatchTier.MANUAL
    assert stored.reason_code is None
    assert stored.reason_detail == "kept"


def test_guarded_writes_refuse_an_unknown_row() -> None:
    assert (
        FakeBroadcastArtistRepository().update_match_status_if_pending(
            uuid4(), MatchStatus.AUTO_MATCHED
        )
        is False
    )
    assert (
        FakeBroadcastTrackIdentityRepository().update_match_status_if_pending(
            uuid4(), MatchStatus.AUTO_MATCHED, MatchTier.NORMALIZATION
        )
        is False
    )


# ---------------------------------------------------------------------------
# G2: artist worker
# ---------------------------------------------------------------------------


def _pending_artist(
    repo: FakeBroadcastArtistRepository, name: str, playlist_id: UUID
) -> BroadcastArtist:
    artist = _artist(repo, name)
    repo.register_playlist_artist(playlist_id, artist.id)
    return artist


def _artist_repos(
    broadcast_artists: FakeBroadcastArtistRepository,
    *,
    catalog: FakeArtistRepository | None = None,
    matches: FakeMatchRepository | None = None,
    identities: FakeBroadcastTrackIdentityRepository | None = None,
) -> ArtistMatchingRepos:
    return ArtistMatchingRepos(
        broadcast_artist_repo=broadcast_artists,
        track_identity_repo=identities or FakeBroadcastTrackIdentityRepository(),
        artist_repo=catalog or FakeArtistRepository(),
        match_repo=matches or FakeMatchRepository(),
        rules_repo=FakeMappingRuleRepository(),
    )


def _metallica_catalog() -> FakeArtistRepository:
    catalog = FakeArtistRepository()
    catalog.upsert(
        Artist(id="mbid-metallica", name="Metallica", sort_name="Metallica", mbid="mbid-metallica")
    )
    return catalog


def test_artist_decided_mid_run_keeps_the_decision_and_gets_no_auto_match() -> None:
    playlist_id = uuid4()
    broadcast_artists = _ArtistsDecidedAfterRead()
    matches = FakeMatchRepository()
    decided = _pending_artist(broadcast_artists, "METALLICA", playlist_id)
    broadcast_artists.decide_after_read[decided.id] = MatchStatus.MANUAL_REJECTED

    match_artists_for_playlist(
        playlist_id=playlist_id,
        repos=_artist_repos(broadcast_artists, catalog=_metallica_catalog(), matches=matches),
        mb_client=FakeMbClient(),
    )

    stored = broadcast_artists.get_by_id(decided.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.MANUAL_REJECTED
    assert matches.get_by_artist(decided.id) is None


def test_artist_decided_mid_run_gets_no_musicbrainz_catalog_upsert() -> None:
    playlist_id = uuid4()
    broadcast_artists = _ArtistsDecidedAfterRead()
    catalog = FakeArtistRepository()
    matches = FakeMatchRepository()
    truncated = "OZZY OSBOURNE THE METAL LEGEND"  # 30 chars: routed to the MB tier
    decided = _pending_artist(broadcast_artists, truncated, playlist_id)
    broadcast_artists.decide_after_read[decided.id] = MatchStatus.MANUAL_MATCHED
    mb_client = FakeMbClient(
        responses={
            truncated: [
                {
                    "id": "mbid-ozzy",
                    "name": "Ozzy Osbourne",
                    "sort-name": "Osbourne, Ozzy",
                    "score": 100,
                },
            ],
        }
    )

    match_artists_for_playlist(
        playlist_id=playlist_id,
        repos=_artist_repos(broadcast_artists, catalog=catalog, matches=matches),
        mb_client=mb_client,
    )

    stored = broadcast_artists.get_by_id(decided.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.MANUAL_MATCHED
    assert matches.get_by_artist(decided.id) is None
    assert catalog.get_by_id("mbid-ozzy") is None


def test_artist_decided_mid_run_does_not_defer_its_songs() -> None:
    playlist_id = uuid4()
    broadcast_artists = _ArtistsDecidedAfterRead()
    identities = FakeBroadcastTrackIdentityRepository()
    decided = _pending_artist(broadcast_artists, "UNKNOWN BAND XYZ", playlist_id)
    song = _identity(identities, decided.id, "Some Song")
    broadcast_artists.decide_after_read[decided.id] = MatchStatus.MANUAL_MATCHED

    match_artists_for_playlist(
        playlist_id=playlist_id,
        repos=_artist_repos(broadcast_artists, identities=identities),
        mb_client=FakeMbClient(),
    )

    stored_song = identities.get_by_id(song.id)
    assert stored_song is not None
    assert stored_song.match_status == MatchStatus.PENDING
    assert stored_song.reason_code is None


def test_only_the_decided_artist_is_left_out_of_the_deferred_cascade() -> None:
    playlist_id = uuid4()
    broadcast_artists = _ArtistsDecidedAfterRead()
    identities = FakeBroadcastTrackIdentityRepository()
    decided = _pending_artist(broadcast_artists, "UNKNOWN BAND XYZ", playlist_id)
    deferred = _pending_artist(broadcast_artists, "ANOTHER UNKNOWN BAND", playlist_id)
    decided_song = _identity(identities, decided.id, "Some Song")
    deferred_song = _identity(identities, deferred.id, "Other Song")
    broadcast_artists.decide_after_read[decided.id] = MatchStatus.MANUAL_MATCHED

    match_artists_for_playlist(
        playlist_id=playlist_id,
        repos=_artist_repos(broadcast_artists, identities=identities),
        mb_client=FakeMbClient(),
    )

    kept = identities.get_by_id(decided_song.id)
    assert kept is not None
    assert kept.match_status == MatchStatus.PENDING
    moved = identities.get_by_id(deferred_song.id)
    assert moved is not None
    assert moved.match_status == MatchStatus.NEEDS_REVIEW
    assert moved.reason_code == ReasonCode.DEFERRED_RETRY


def test_artist_not_decided_mid_run_is_written_as_usual() -> None:
    playlist_id = uuid4()
    broadcast_artists = _ArtistsDecidedAfterRead()
    matches = FakeMatchRepository()
    decided = _pending_artist(broadcast_artists, "UNKNOWN BAND XYZ", playlist_id)
    untouched = _pending_artist(broadcast_artists, "METALLICA", playlist_id)
    broadcast_artists.decide_after_read[decided.id] = MatchStatus.MANUAL_REJECTED

    match_artists_for_playlist(
        playlist_id=playlist_id,
        repos=_artist_repos(broadcast_artists, catalog=_metallica_catalog(), matches=matches),
        mb_client=FakeMbClient(),
    )

    stored = broadcast_artists.get_by_id(untouched.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    created = matches.get_by_artist(untouched.id)
    assert created is not None
    assert created.target_id == "mbid-metallica"


# ---------------------------------------------------------------------------
# G3: identity worker (all three write sites)
# ---------------------------------------------------------------------------


def _library_file(path: str, title: str, work_id: str) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=path,
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        work_id=work_id,
        audio=AudioMetadata(
            track_title=title,
            normalized_title=normalize_title(title),
            artist_name="Metallica",
            normalized_artist_name="metallica",
        ),
    )


class _IdentityRun:
    """One playlist, one Metallica artist, and rule-matched songs ready to auto-match."""

    def __init__(self, *, register_artist: bool = True) -> None:
        self.playlist_id = uuid4()
        self.artists = FakeBroadcastArtistRepository()
        self.identities = _IdentitiesDecidedAfterRead()
        self.matches = FakeMatchRepository()
        self.files = FakeLibraryFileRepository()
        self.rules = FakeMappingRuleRepository()
        self.files_by_title: dict[str, LibraryFile] = {}
        artist = BroadcastArtist(
            id=uuid4(), original_name="Metallica", normalized_name=normalize_artist("Metallica")
        )
        self.artist_id = artist.id
        if register_artist:
            self.artists.upsert(artist)

    def song(self, title: str, work_id: str) -> BroadcastTrackIdentity:
        identity = _identity(self.identities, self.artist_id, title)
        self.identities.register_playlist_identity(self.playlist_id, identity.id)
        library_file = _library_file(f"/music/metallica/{work_id}.flac", title, work_id)
        self.files.upsert(library_file)
        self.files_by_title[title] = library_file
        self.rules.create(
            MappingRule(
                id=uuid4(),
                source_pattern=identity.normalized_signature,
                target_type=TargetType.LIBRARY_FILE,
                target_id=library_file.file_path,
                priority=10,
            )
        )
        return identity

    def run(self) -> list[str]:
        return match_identities_for_playlist(
            playlist_id=self.playlist_id,
            repos=IdentityMatchingRepos(
                track_identity_repo=self.identities,
                broadcast_artist_repo=self.artists,
                match_repo=self.matches,
                library_file_repo=self.files,
                rules_repo=self.rules,
                catalog_repo=FakeArtistRepository(),
            ),
            mb_client=FakeMbClient(),
        )


def test_identity_decided_mid_run_keeps_the_decision_and_gets_no_match() -> None:
    run = _IdentityRun()
    decided = run.song("Enter Sandman", "work-sandman")
    untouched = run.song("One", "work-one")
    run.identities.decide_after_read[decided.id] = MatchStatus.MANUAL_REJECTED

    work_ids = run.run()

    stored = run.identities.get_by_id(decided.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.MANUAL_REJECTED
    assert run.matches.get_by_identity(decided.id) is None
    # Only the untouched song is auto-matched and handed on for master recalculation.
    assert work_ids == ["work-one"]
    other = run.identities.get_by_id(untouched.id)
    assert other is not None
    assert other.match_status == MatchStatus.AUTO_MATCHED


def test_orphaned_identity_decided_mid_run_keeps_the_decision() -> None:
    run = _IdentityRun(register_artist=False)
    decided = run.song("Enter Sandman", "work-sandman")
    run.identities.decide_after_read[decided.id] = MatchStatus.MANUAL_MATCHED

    run.run()

    stored = run.identities.get_by_id(decided.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.MANUAL_MATCHED
    assert stored.reason_code is None


def test_exhausted_engine_identity_decided_mid_run_keeps_the_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.services.identity_matching_service.IdentityMatchingEngine.resolve",
        lambda self, identity, artist: None,
    )
    run = _IdentityRun()
    decided = run.song("Enter Sandman", "work-sandman")
    run.identities.decide_after_read[decided.id] = MatchStatus.MANUAL_MATCHED

    run.run()

    stored = run.identities.get_by_id(decided.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.MANUAL_MATCHED
    assert stored.reason_code is None


def test_weak_candidate_identity_decided_mid_run_gets_no_match_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NEEDS_REVIEW result with a candidate normally persists a match row for the review UI;
    not when the user decided the song while the worker ran."""
    run = _IdentityRun()
    decided = run.song("Enter Sandman", "work-sandman")
    candidate = run.files_by_title["Enter Sandman"]
    monkeypatch.setattr(
        "backend.services.identity_matching_service.IdentityMatchingEngine.resolve",
        lambda self, identity, artist: IdentityMatchResult(
            status=MatchStatus.NEEDS_REVIEW,
            tier=MatchTier.LOCAL_FILE_FUZZY,
            confidence_score=55.0,
            library_file_id=candidate.id,
            reason_code=ReasonCode.LOW_CONFIDENCE,
        ),
    )
    run.identities.decide_after_read[decided.id] = MatchStatus.MANUAL_REJECTED

    run.run()

    stored = run.identities.get_by_id(decided.id)
    assert stored is not None
    assert stored.match_status == MatchStatus.MANUAL_REJECTED
    assert run.matches.get_by_identity(decided.id) is None
