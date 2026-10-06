"""Acceptance tests: a new identity-matching result replaces the song's old match rows.

Spec 2026-10-05 §4.2 [review-B]: the re-check rewinds undecided songs to pending and KEEPS their
match rows, so match_identities_for_playlist calls match_repo.delete_for_identity after every
successful guarded write (normal result, orphaned identity, exhausted engine) and before any
create. A result without a file still clears the old suggestion. A refused guarded write (the
user decided meanwhile, AUD-R018) deletes nothing.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, ReasonCode
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import Match
from backend.services.identity_matching_service import (
    IdentityMatchingRepos,
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

ARTIST = "Metallica"
NORM = normalize_artist(ARTIST)
TITLE = "Enter Sandman"


class _Matches(FakeMatchRepository):
    """Records deletes and creates in order, and lists every row of one song."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, UUID | None]] = []

    def create(self, match: Match) -> Match:
        self.calls.append(("create", match.identity_id))
        return super().create(match)

    def delete_for_identity(self, identity_id: UUID) -> None:
        self.calls.append(("delete", identity_id))
        super().delete_for_identity(identity_id)

    def rows_for(self, identity_id: UUID) -> list[Match]:
        return [m for m in self._data.values() if m.identity_id == identity_id]


class _SongsDecidedAfterRead(FakeBroadcastTrackIdentityRepository):
    """The user decides some songs just after the worker has read them as PENDING."""

    def __init__(self) -> None:
        super().__init__()
        self.decide_after_read: dict[UUID, MatchStatus] = {}

    def get_pending_for_playlist(self, playlist_id: UUID) -> list[BroadcastTrackIdentity]:
        snapshot = super().get_pending_for_playlist(playlist_id)
        for identity_id, decision in self.decide_after_read.items():
            self.update_match_status(identity_id, decision, MatchTier.MANUAL)
        return snapshot


class _Run:
    """One playlist and one Metallica artist; every song starts with an old suggestion."""

    def __init__(self, *, with_artist: bool = True) -> None:
        self.playlist_id = uuid4()
        self.artists = FakeBroadcastArtistRepository()
        self.songs = _SongsDecidedAfterRead()
        self.matches = _Matches()
        self.files = FakeLibraryFileRepository()
        self.artist = BroadcastArtist(id=uuid4(), original_name=ARTIST, normalized_name=NORM)
        if with_artist:
            self.artists.upsert(self.artist)

    def file(self, title: str = TITLE) -> LibraryFile:
        library_file = LibraryFile(
            id=uuid4(),
            file_path=f"/music/{uuid4().hex}.flac",
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
            audio=AudioMetadata(
                track_title=title,
                normalized_title=normalize_title(title),
                artist_name=ARTIST,
                normalized_artist_name=NORM,
            ),
        )
        self.files.upsert(library_file)
        return library_file

    def song(self, title: str = TITLE) -> tuple[BroadcastTrackIdentity, Match]:
        norm = normalize_title(title)
        song = self.songs.upsert(
            BroadcastTrackIdentity(
                id=uuid4(),
                broadcast_artist_id=self.artist.id,
                original_title=title,
                normalized_title=norm,
                normalized_signature=compute_normalized_signature(NORM, norm),
            )
        )
        self.songs.register_playlist_identity(self.playlist_id, song.id)
        # A rewound song keeps its old suggestion: the rewind is status only (spec §4.2).
        old = self.matches.create(
            Match(
                id=uuid4(),
                identity_id=song.id,
                library_file_id=uuid4(),
                confidence_score=60.0,
                match_tier=MatchTier.LOCAL_FILE_FUZZY,
            )
        )
        self.matches.calls.clear()
        return song, old

    def stored(self, song: BroadcastTrackIdentity) -> BroadcastTrackIdentity:
        stored = self.songs.get_by_id(song.id)
        assert stored is not None
        return stored

    def run(self) -> None:
        match_identities_for_playlist(
            playlist_id=self.playlist_id,
            repos=IdentityMatchingRepos(
                track_identity_repo=self.songs,
                broadcast_artist_repo=self.artists,
                match_repo=self.matches,
                library_file_repo=self.files,
                rules_repo=FakeMappingRuleRepository(),
                catalog_repo=FakeArtistRepository(),
            ),
            mb_client=FakeMbClient(),
        )


def test_a_new_suggestion_replaces_the_old_one() -> None:
    run = _Run()
    new = run.file()
    song, _ = run.song()

    run.run()

    assert [m.library_file_id for m in run.matches.rows_for(song.id)] == [new.id]


def test_old_rows_are_deleted_before_the_new_one_is_created() -> None:
    run = _Run()
    run.file()
    song, _ = run.song()

    run.run()

    assert run.matches.calls == [("delete", song.id), ("create", song.id)]


def test_a_result_without_a_file_clears_the_old_suggestion() -> None:
    run = _Run()  # no library file at all: nothing to suggest
    song, _ = run.song()

    run.run()

    assert run.matches.rows_for(song.id) == []
    assert run.stored(song).match_status != MatchStatus.PENDING


def test_an_orphaned_song_loses_its_old_suggestion() -> None:
    run = _Run(with_artist=False)
    song, _ = run.song()

    run.run()

    assert run.matches.rows_for(song.id) == []
    assert run.stored(song).reason_code == ReasonCode.ORPHANED_IDENTITY


def test_an_exhausted_engine_clears_the_old_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.services.identity_matching_service.IdentityMatchingEngine.resolve",
        lambda self, identity, artist: None,
    )
    run = _Run()
    run.file()
    song, _ = run.song()

    run.run()

    assert run.matches.rows_for(song.id) == []
    assert run.stored(song).reason_code == ReasonCode.NO_CANDIDATES


@pytest.mark.parametrize("decision", [MatchStatus.MANUAL_MATCHED, MatchStatus.MANUAL_REJECTED])
def test_a_song_decided_meanwhile_keeps_its_match_row(decision: MatchStatus) -> None:
    run = _Run()
    run.file()
    song, old = run.song()
    run.songs.decide_after_read[song.id] = decision

    run.run()

    assert run.matches.rows_for(song.id) == [old]
    assert run.matches.calls == []
