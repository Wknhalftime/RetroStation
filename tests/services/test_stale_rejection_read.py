"""A matching run must not suggest a file the curator rejected while the run was going.

Found by the TLA+ model (docs/tla/MatchingStatus.tla, config MC_w1_link.cfg, property
NoStaleWrite), with one worker:

1. a song-matching run reads the playlist's pending songs, rejected files included
   (``get_pending_for_playlist``; the exclusions are built once, ``build_song_engines``);
2. before the run reaches song X, the curator rejects X's file F (X becomes manual_rejected,
   F joins ``rejected_file_ids``);
3. the curator then links X's artist, whose cascade sends X back to pending
   (``routers/matching.py``, the MANUAL_MATCHED artist resolve);
4. the run reaches X. Its guarded write sees pending and lands, with a result scored from the
   read in step 1, where F was not yet rejected.

The curator's two API writes are simulated on the fake repository at the run's first progress
tick (the same hook as tests/services/test_matching_progress_ticks.py). Only their effect on
the song row is modelled; the artist row is left as the run read it.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import EnrichmentStatus, MatchStatus
from backend.domain.library import AudioMetadata, LibraryFile
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


class _CuratorSongs(FakeBroadcastTrackIdentityRepository):
    """The fake, plus the song-row effect of the API's Reject (identity_resolution_service)."""

    def reject_file(self, identity_id: UUID, file_id: UUID) -> None:
        song = self._data[identity_id]
        self._data[identity_id] = replace(
            song,
            match_status=MatchStatus.MANUAL_REJECTED,
            match_tier=None,
            rejected_file_ids=(*song.rejected_file_ids, file_id),
        )


def _file(title: str, work_id: str) -> LibraryFile:
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
        ),
    )


def _song(artist: BroadcastArtist, title: str) -> BroadcastTrackIdentity:
    norm_title = normalize_title(title)
    return BroadcastTrackIdentity(
        id=uuid4(),
        broadcast_artist_id=artist.id,
        original_title=title,
        normalized_title=norm_title,
        normalized_signature=compute_normalized_signature(NORM, norm_title),
    )


class _CuratorRejectsThenLinks:
    """At the first tick, the curator rejects the still-pending song's file, then links its
    artist (the cascade rewinds the song to pending)."""

    def __init__(
        self, songs: _CuratorSongs, artist_id: UUID, file_by_song: dict[UUID, LibraryFile]
    ) -> None:
        self._songs = songs
        self._artist_id = artist_id
        self._file_by_song = file_by_song
        self.ticks = 0
        self.victim: UUID | None = None

    def tick(self) -> None:
        self.ticks += 1
        if self.ticks != 1:
            return
        for song_id, file in self._file_by_song.items():
            stored = self._songs.get_by_id(song_id)
            if stored is not None and stored.match_status == MatchStatus.PENDING:
                self.victim = song_id
                self._songs.reject_file(song_id, file.id)
        # The artist link cascade: undecided songs (manual_rejected included) to pending.
        self._songs.rewind_undecided([self._artist_id])


def test_a_file_rejected_during_the_run_is_not_suggested_for_its_song() -> None:
    playlist_id = uuid4()
    artists = FakeBroadcastArtistRepository()
    artist = artists.upsert(BroadcastArtist(id=uuid4(), original_name=ARTIST, normalized_name=NORM))
    songs = _CuratorSongs()
    files = FakeLibraryFileRepository()
    file_by_song: dict[UUID, LibraryFile] = {}
    for title, work_id in (("Enter Sandman", "w1"), ("The Unforgiven", "w2")):
        song = songs.upsert(_song(artist, title))
        songs.register_playlist_identity(playlist_id, song.id)
        file_by_song[song.id] = files.upsert(_file(title, work_id))
    matches = FakeMatchRepository()
    curator = _CuratorRejectsThenLinks(songs, artist.id, file_by_song)

    match_identities_for_playlist(
        playlist_id=playlist_id,
        repos=IdentityMatchingRepos(
            track_identity_repo=songs,
            broadcast_artist_repo=artists,
            match_repo=matches,
            library_file_repo=files,
            rules_repo=FakeMappingRuleRepository(),
            catalog_repo=FakeArtistRepository(),
        ),
        mb_client=FakeMbClient(),
        progress=curator,
    )

    # The setup did what the docstring says: one song was still pending at the first tick,
    # its file was rejected, and the cascade put it back to pending before the run reached it.
    assert curator.victim is not None
    rejected = file_by_song[curator.victim]
    victim = songs.get_by_id(curator.victim)
    assert victim is not None
    assert victim.rejected_file_ids == (rejected.id,)

    suggestion = matches.get_by_identity(curator.victim)
    assert suggestion is None or suggestion.library_file_id != rejected.id, (
        f"the run suggested the file the curator rejected meanwhile "
        f"(song now {victim.match_status.value})"
    )


def test_control_without_the_curator_each_song_is_suggested_its_own_file() -> None:
    """The same playlist with no curator action: the matcher does suggest each song's file,
    so the test above can only pass by honouring the rejection, not by missing the file."""
    playlist_id = uuid4()
    artists = FakeBroadcastArtistRepository()
    artist = artists.upsert(BroadcastArtist(id=uuid4(), original_name=ARTIST, normalized_name=NORM))
    songs = _CuratorSongs()
    files = FakeLibraryFileRepository()
    file_by_song: dict[UUID, LibraryFile] = {}
    for title, work_id in (("Enter Sandman", "w1"), ("The Unforgiven", "w2")):
        song = songs.upsert(_song(artist, title))
        songs.register_playlist_identity(playlist_id, song.id)
        file_by_song[song.id] = files.upsert(_file(title, work_id))
    matches = FakeMatchRepository()

    match_identities_for_playlist(
        playlist_id=playlist_id,
        repos=IdentityMatchingRepos(
            track_identity_repo=songs,
            broadcast_artist_repo=artists,
            match_repo=matches,
            library_file_repo=files,
            rules_repo=FakeMappingRuleRepository(),
            catalog_repo=FakeArtistRepository(),
        ),
        mb_client=FakeMbClient(),
    )

    for song_id, file in file_by_song.items():
        suggestion = matches.get_by_identity(song_id)
        assert suggestion is not None
        assert suggestion.library_file_id == file.id
