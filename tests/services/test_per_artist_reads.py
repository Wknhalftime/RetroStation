"""A matching stage reads each artist's lookups once, not once per song (profile 2026-10-18).

match_identities_for_playlist used to repeat four reads for every song: the artist's match
row, its catalog artist, its files by artist MBID and its files by name. On the bench (22k
songs, 6.2k artists) that was 444k file rows. Now they are read once per artist in a stage,
and a file's scoring titles are worked out once per title, not once per song x file.
"""

from __future__ import annotations

from collections import Counter
from uuid import UUID, uuid4

import pytest

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.catalog import Artist
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, TargetType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import Match
from backend.services import title_scoring
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

_METALLICA_MBID = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"


class _CountingFiles(FakeLibraryFileRepository):
    def __init__(self) -> None:
        super().__init__()
        self.by_mbid: Counter[str] = Counter()
        self.by_name: Counter[str] = Counter()

    def get_by_artist_mbid(self, artist_mbid: str) -> list[LibraryFile]:
        self.by_mbid[artist_mbid] += 1
        return super().get_by_artist_mbid(artist_mbid)

    def get_by_normalized_artist_name(
        self, normalized_name: str, limit: int = 100
    ) -> list[LibraryFile]:
        self.by_name[normalized_name] += 1
        return super().get_by_normalized_artist_name(normalized_name, limit)


class _CountingMatches(FakeMatchRepository):
    def __init__(self) -> None:
        super().__init__()
        self.by_artist: Counter[UUID] = Counter()

    def get_by_artist(self, artist_id: UUID) -> Match | None:
        self.by_artist[artist_id] += 1
        return super().get_by_artist(artist_id)


class _CountingCatalog(FakeArtistRepository):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: Counter[str] = Counter()

    def get_by_id(self, artist_id: str) -> Artist | None:
        self.by_id[artist_id] += 1
        return super().get_by_id(artist_id)


class _Stage:
    """One playlist's repos, with a resolved artist (Metallica) and an unresolved one (ABBA)."""

    def __init__(self) -> None:
        self.playlist_id = uuid4()
        self.identities = FakeBroadcastTrackIdentityRepository()
        self.artists = FakeBroadcastArtistRepository()
        self.matches = _CountingMatches()
        self.files = _CountingFiles()
        self.catalog = _CountingCatalog()
        self.metallica = self._artist("Metallica", MatchStatus.AUTO_MATCHED)
        self.abba = self._artist("ABBA", MatchStatus.PENDING)
        self.matches.create(
            Match(
                id=uuid4(),
                artist_id=self.metallica.id,
                target_id=_METALLICA_MBID,
                target_type=TargetType.ARTIST,
                confidence_score=100.0,
                match_tier=MatchTier.MUSICBRAINZ_ID_EXACT,
            )
        )

    def _artist(self, name: str, status: MatchStatus) -> BroadcastArtist:
        artist = BroadcastArtist(
            id=uuid4(),
            original_name=name,
            normalized_name=normalize_artist(name),
            match_status=status,
        )
        self.artists.upsert(artist)
        return artist

    def file(self, artist: BroadcastArtist, title: str, *, tagged: bool = False) -> LibraryFile:
        f = LibraryFile(
            id=uuid4(),
            file_path=f"/music/{artist.normalized_name}/{uuid4().hex}.flac",
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
            audio=AudioMetadata(
                artist_mbid=_METALLICA_MBID if tagged else None,
                track_title=title,
                normalized_title=normalize_title(title),
                artist_name=artist.original_name,
                normalized_artist_name=artist.normalized_name,
            ),
        )
        self.files.upsert(f)
        return f

    def song(self, artist: BroadcastArtist, title: str) -> BroadcastTrackIdentity:
        norm_title = normalize_title(title)
        identity = BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title=title,
            normalized_title=norm_title,
            normalized_signature=compute_normalized_signature(artist.normalized_name, norm_title),
        )
        self.identities.upsert(identity)
        self.identities.register_playlist_identity(self.playlist_id, identity.id)
        return identity

    def run(self) -> list[str]:
        return match_identities_for_playlist(
            playlist_id=self.playlist_id,
            repos=IdentityMatchingRepos(
                track_identity_repo=self.identities,
                broadcast_artist_repo=self.artists,
                match_repo=self.matches,
                library_file_repo=self.files,
                rules_repo=FakeMappingRuleRepository(),
                catalog_repo=self.catalog,
            ),
            mb_client=FakeMbClient(),
        )

    def matched_file(self, song: BroadcastTrackIdentity) -> UUID | None:
        stored = self.identities.get_by_id(song.id)
        assert stored is not None
        if stored.match_status != MatchStatus.AUTO_MATCHED:
            return None
        match = self.matches.get_by_identity(song.id)
        return match.library_file_id if match else None


def test_each_artists_reads_happen_once_however_many_of_its_songs_the_stage_has() -> None:
    stage = _Stage()
    sandman = stage.file(stage.metallica, "Enter Sandman", tagged=True)
    one = stage.file(stage.metallica, "One", tagged=True)
    waterloo = stage.file(stage.abba, "Waterloo")
    fernando = stage.file(stage.abba, "Fernando")
    # Interleaved, as a playlist's songs arrive; "Battery" has no file, so it reaches Step C.
    songs = [
        stage.song(stage.metallica, "Enter Sandman"),
        stage.song(stage.abba, "Waterloo"),
        stage.song(stage.metallica, "One"),
        stage.song(stage.abba, "Fernando"),
        stage.song(stage.metallica, "Battery"),
    ]

    stage.run()

    assert stage.files.by_mbid == Counter({_METALLICA_MBID: 1})
    assert stage.files.by_name == Counter({"metallica": 1, "abba": 1})
    assert stage.matches.by_artist == Counter({stage.metallica.id: 1})
    assert stage.catalog.by_id == Counter({_METALLICA_MBID: 1})
    assert [stage.matched_file(s) for s in songs] == [
        sandman.id,
        waterloo.id,
        one.id,
        fernando.id,
        None,
    ]


def test_the_next_stage_reads_the_artist_afresh() -> None:
    stage = _Stage()
    stage.file(stage.abba, "Waterloo")
    stage.song(stage.abba, "Waterloo")
    stage.run()
    fernando = stage.file(stage.abba, "Fernando")
    song = stage.song(stage.abba, "Fernando")

    stage.run()

    assert stage.files.by_name == Counter({"abba": 2})
    assert stage.matched_file(song) == fernando.id


def test_a_files_scoring_titles_are_worked_out_once_for_all_the_songs_scored_against_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: Counter[str | None] = Counter()
    real = title_scoring.library_title_variants

    def counting(track_title: str | None, normalized_title: str | None) -> tuple[str, ...]:
        calls[track_title] += 1
        return real(track_title, normalized_title)

    monkeypatch.setattr(title_scoring, "library_title_variants", counting)
    stage = _Stage()
    # Titles no other test uses, so nothing earlier has already worked them out.
    tag = uuid4().hex[:8]
    titles = [f"Dancing Queen {tag}", f"Mamma Mia {tag}", f"SOS {tag}"]
    for title in titles:
        stage.file(stage.abba, title)
    for title in titles:
        stage.song(stage.abba, title)

    stage.run()

    assert calls == Counter({title: 1 for title in titles})
