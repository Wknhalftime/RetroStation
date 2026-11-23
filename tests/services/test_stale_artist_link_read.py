"""An artist link made while a matching run is going must reach the songs the run had read.

A lead from the TLA+ model (docs/tla/README.md, "A lead, not a finding"), with one worker:

1. a song-matching run reads the playlist's pending songs and, once, their artists
   (``match_identities_for_playlist``: ``artists_by_id``, read at stage start);
2. before the run reaches song X, the curator links X's artist (the MANUAL_MATCHED artist
   resolve in ``routers/matching.py``): the artist row and its match row change, undecided
   songs go back to pending, pending songs lose their match rows, and identity matching is
   enqueued for the playlist;
3. the run reaches X and scores it from the artist it read in step 1, still unresolved: the
   name fallback (``BroadcastToLocalStrategy``) instead of the MBID path;
4. the link's own job runs next, but reads only pending songs, so it skips X.

Why the artist read changes the result here: an unresolved artist's songs are scored against
every file with the artist's name (the name path). A linked artist's songs are first scored
against the files tagged with its MBID (Step A), and Step A's result is kept on a tie with the
name path (AUD-R022 D13). Each song has two near titles that score the same, inside the band
where the lead decides (80 to 95): "Rich Girl" scores 88.9 against "Rick Girl" (tagged) and
"Rich Gurl" (untagged). Step A sees the tagged one with a clear lead and auto-matches; the name
path sees a tie and sends the song to review (AMBIGUOUS_GAP).

The curator's link is simulated on the fakes at the run's first progress tick (the hook
tests/services/test_stale_rejection_read.py uses). The fake artist repository stores a new
row on update, so the run keeps the artist it read, as the real stage-start read does.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, TargetType
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

ARTIST = "Hall & Oates"
NORM = normalize_artist(ARTIST)
ARTIST_MBID = "a0b5a6c4-0000-4000-8000-000000000001"
# Each song: (the file tagged with the artist MBID, an untagged file scoring the same).
SONGS = {
    "Rich Girl": ("Rick Girl", "Rich Gurl"),  # 88.9 each
    "Sara Smile": ("Sara Smyle", "Sara Smilr"),  # 90.0 each
}
OTHER_TAGGED = "Maneater"  # far from both titles: Step A's runner-up, so its lead is real


class _Library:
    """One playlist, one unresolved artist, and for each song two near titles that tie."""

    def __init__(self) -> None:
        self.playlist_id = uuid4()
        self.artists = FakeBroadcastArtistRepository()
        self.artist = self.artists.upsert(
            BroadcastArtist(id=uuid4(), original_name=ARTIST, normalized_name=NORM)
        )
        self.songs = FakeBroadcastTrackIdentityRepository()
        self.files = FakeLibraryFileRepository()
        self.matches = FakeMatchRepository()
        self.tagged_file: dict[UUID, LibraryFile] = {}
        for title, (tagged, untagged) in SONGS.items():
            song = self.songs.upsert(_song(self.artist, title))
            self.songs.register_playlist_identity(self.playlist_id, song.id)
            self.tagged_file[song.id] = self.files.upsert(_file(tagged, ARTIST_MBID))
            self.files.upsert(_file(untagged, None))
        self.files.upsert(_file(OTHER_TAGGED, ARTIST_MBID))

    def repos(self) -> IdentityMatchingRepos:
        return IdentityMatchingRepos(
            track_identity_repo=self.songs,
            broadcast_artist_repo=self.artists,
            match_repo=self.matches,
            library_file_repo=self.files,
            rules_repo=FakeMappingRuleRepository(),
            catalog_repo=FakeArtistRepository(),
        )

    def run(self, progress: _CuratorLinksArtist | None = None) -> None:
        """One identity-matching job for the playlist (what the link enqueues)."""
        if progress is None:
            match_identities_for_playlist(
                playlist_id=self.playlist_id, repos=self.repos(), mb_client=FakeMbClient()
            )
        else:
            match_identities_for_playlist(
                playlist_id=self.playlist_id,
                repos=self.repos(),
                mb_client=FakeMbClient(),
                progress=progress,
            )

    def link_artist(self) -> None:
        """The MANUAL_MATCHED artist resolve's writes (routers/matching.py resolve_artist)."""
        self.artists.update_match_status(self.artist.id, MatchStatus.MANUAL_MATCHED)
        self.matches.create(
            Match(
                id=uuid4(),
                artist_id=self.artist.id,
                target_id=ARTIST_MBID,
                target_type=TargetType.ARTIST,
                confidence_score=1.0,
                match_tier=MatchTier.MANUAL,
            )
        )
        self.songs.rewind_undecided([self.artist.id])
        for song_id in self.tagged_file:
            stored = self.songs.get_by_id(song_id)
            if stored is not None and stored.match_status == MatchStatus.PENDING:
                self.matches.delete_for_identity(song_id)


def _file(title: str, artist_mbid: str | None) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4().hex}.flac",
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        work_id=None,
        audio=AudioMetadata(
            track_title=title,
            normalized_title=normalize_title(title),
            artist_name=ARTIST,
            normalized_artist_name=NORM,
            artist_mbid=artist_mbid,
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


class _CuratorLinksArtist:
    """At the first tick, the curator links the artist; the song still pending is the victim."""

    def __init__(self, library: _Library) -> None:
        self._library = library
        self.ticks = 0
        self.victim: UUID | None = None

    def tick(self) -> None:
        self.ticks += 1
        if self.ticks != 1:
            return
        for song_id in self._library.tagged_file:
            stored = self._library.songs.get_by_id(song_id)
            if stored is not None and stored.match_status == MatchStatus.PENDING:
                self.victim = song_id
        self._library.link_artist()


def _status(library: _Library, song_id: UUID) -> MatchStatus:
    song = library.songs.get_by_id(song_id)
    assert song is not None
    return song.match_status


def test_a_song_read_before_an_artist_link_is_matched_as_the_link_says() -> None:
    library = _Library()
    curator = _CuratorLinksArtist(library)

    library.run(progress=curator)  # the run the link interrupts
    library.run()  # the job the link enqueued

    # The setup did what the docstring says: one song was still pending when the curator
    # linked the artist, so the interrupted run decided it after the link.
    assert curator.victim is not None
    victim = curator.victim
    suggestion = library.matches.get_by_identity(victim)
    assert _status(library, victim) == MatchStatus.AUTO_MATCHED, (
        f"the song the run read before the link ended {_status(library, victim).value}, "
        f"suggested {None if suggestion is None else suggestion.library_file_id}; linked first, "
        f"it auto-matches {library.tagged_file[victim].id}"
    )
    assert suggestion is not None
    assert suggestion.library_file_id == library.tagged_file[victim].id


def test_control_linked_before_the_run_every_song_auto_matches_its_tagged_copy() -> None:
    """What the link should give: Step A's clear lead on the tagged file."""
    library = _Library()
    library.link_artist()

    library.run()

    for song_id, tagged in library.tagged_file.items():
        assert _status(library, song_id) == MatchStatus.AUTO_MATCHED
        suggestion = library.matches.get_by_identity(song_id)
        assert suggestion is not None
        assert suggestion.library_file_id == tagged.id


def test_control_never_linked_every_song_goes_to_review() -> None:
    """What the pre-link read gives: the name path sees a tie, so no song auto-matches. The
    first test can only pass by using the link, not by the name path."""
    library = _Library()

    library.run()

    for song_id in library.tagged_file:
        assert _status(library, song_id) == MatchStatus.NEEDS_REVIEW
