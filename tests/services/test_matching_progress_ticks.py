"""Acceptance tests: the matching services tell a progress observer about each settled item
(spec 2026-10-17 "Matching run progress").

``match_artists_for_playlist`` and ``match_identities_for_playlist`` take ``progress``, a
``backend.services.matching_progress.MatchingProgress`` (one method, ``tick()``), with a
no-op default (``NO_PROGRESS``). Each pending item ticks exactly once, after its result is
written: at the k-th tick, k items are no longer pending. The observer changes nothing the
services decide (the existing service tests run on the default).
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, MatchTier
from backend.services.artist_matching_service import ArtistMatchingRepos, match_artists_for_playlist
from backend.services.identity_matching_service import (
    IdentityMatchingRepos,
    match_identities_for_playlist,
)
from backend.services.matching_progress import NO_PROGRESS, MatchingProgress
from backend.services.normalization import normalize_artist
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient


class _Recorder:
    """Records, at each tick, how many items are no longer pending."""

    def __init__(self, settled: Callable[[], int]) -> None:
        self._settled = settled
        self.settled_at_tick: list[int] = []

    def tick(self) -> None:
        self.settled_at_tick.append(self._settled())


def test_the_no_op_observer_is_a_matching_progress() -> None:
    progress: MatchingProgress = NO_PROGRESS
    progress.tick()


# --- songs ---------------------------------------------------------------------------------


def _song(
    songs: FakeBroadcastTrackIdentityRepository, playlist_id: UUID, artist_id: UUID
) -> BroadcastTrackIdentity:
    title = uuid4().hex
    song = songs.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist_id,
            original_title=title,
            normalized_title=title,
            normalized_signature=f"x:{title}",
        )
    )
    songs.register_playlist_identity(playlist_id, song.id)
    return song


def _identity_repos(
    songs: FakeBroadcastTrackIdentityRepository, artists: FakeBroadcastArtistRepository
) -> IdentityMatchingRepos:
    return IdentityMatchingRepos(
        track_identity_repo=songs,
        broadcast_artist_repo=artists,
        match_repo=FakeMatchRepository(),
        library_file_repo=FakeLibraryFileRepository(),
        rules_repo=FakeMappingRuleRepository(),
        catalog_repo=FakeArtistRepository(),
    )


def test_song_matching_ticks_once_per_song_after_writing_it() -> None:
    playlist_id = uuid4()
    songs = FakeBroadcastTrackIdentityRepository()
    artists = FakeBroadcastArtistRepository()
    artist = artists.upsert(
        BroadcastArtist(
            id=uuid4(),
            original_name="Nobody Known",
            normalized_name=normalize_artist("Nobody Known"),
            match_status=MatchStatus.NEEDS_REVIEW,
        )
    )
    all_songs = [_song(songs, playlist_id, artist.id) for _ in range(2)]
    all_songs.append(_song(songs, playlist_id, uuid4()))  # an orphan: no artist row

    def settled() -> int:
        return sum(
            1
            for s in all_songs
            if (stored := songs.get_by_id(s.id)) is not None
            and stored.match_status != MatchStatus.PENDING
        )

    recorder = _Recorder(settled)
    match_identities_for_playlist(
        playlist_id=playlist_id,
        repos=_identity_repos(songs, artists),
        mb_client=FakeMbClient(),
        progress=recorder,
    )

    assert recorder.settled_at_tick == [1, 2, 3]


def test_a_song_the_user_decided_meanwhile_still_ticks_once() -> None:
    playlist_id = uuid4()
    songs = FakeBroadcastTrackIdentityRepository()
    artists = FakeBroadcastArtistRepository()
    artist = artists.upsert(
        BroadcastArtist(
            id=uuid4(),
            original_name="Nobody Known",
            normalized_name=normalize_artist("Nobody Known"),
            match_status=MatchStatus.NEEDS_REVIEW,
        )
    )
    all_songs = [_song(songs, playlist_id, artist.id) for _ in range(3)]

    class _UserDecidesMeanwhile:
        """At the first tick, the user decides every song still pending (AUD-R018): the
        worker's guarded writes for them change nothing."""

        def __init__(self) -> None:
            self.ticks = 0

        def tick(self) -> None:
            self.ticks += 1
            if self.ticks == 1:
                for s in all_songs:
                    stored = songs.get_by_id(s.id)
                    if stored is not None and stored.match_status == MatchStatus.PENDING:
                        songs.update_match_status(
                            s.id, MatchStatus.MANUAL_MATCHED, MatchTier.MANUAL
                        )

    recorder = _UserDecidesMeanwhile()
    match_identities_for_playlist(
        playlist_id=playlist_id,
        repos=_identity_repos(songs, artists),
        mb_client=FakeMbClient(),
        progress=recorder,
    )

    assert recorder.ticks == 3


def test_song_matching_with_nothing_pending_never_ticks() -> None:
    recorder = _Recorder(lambda: 0)

    match_identities_for_playlist(
        playlist_id=uuid4(),
        repos=_identity_repos(
            FakeBroadcastTrackIdentityRepository(), FakeBroadcastArtistRepository()
        ),
        mb_client=FakeMbClient(),
        progress=recorder,
    )

    assert recorder.settled_at_tick == []


# --- artists -------------------------------------------------------------------------------


def test_artist_matching_ticks_once_per_artist_after_writing_it() -> None:
    playlist_id = uuid4()
    artists = FakeBroadcastArtistRepository()
    pending: list[BroadcastArtist] = []
    for name in ("Unknown One", "Unknown Two", "Unknown Three"):
        artist = artists.upsert(
            BroadcastArtist(id=uuid4(), original_name=name, normalized_name=normalize_artist(name))
        )
        artists.register_playlist_artist(playlist_id, artist.id)
        pending.append(artist)

    def settled() -> int:
        return sum(
            1
            for a in pending
            if (stored := artists.get_by_id(a.id)) is not None
            and stored.match_status != MatchStatus.PENDING
        )

    recorder = _Recorder(settled)
    match_artists_for_playlist(
        playlist_id=playlist_id,
        repos=ArtistMatchingRepos(
            broadcast_artist_repo=artists,
            track_identity_repo=FakeBroadcastTrackIdentityRepository(),
            artist_repo=FakeArtistRepository(),
            match_repo=FakeMatchRepository(),
            rules_repo=FakeMappingRuleRepository(),
        ),
        mb_client=FakeMbClient(),
        progress=recorder,
    )

    assert recorder.settled_at_tick == [1, 2, 3]
