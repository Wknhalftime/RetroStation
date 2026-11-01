"""Acceptance: song matching skips a MusicBrainz recording search that cannot change a song.

Profile 2026-10-18: Tier 1 Step B (search_recording) ran 11,778 times in a warm full run and
won 0 songs; on the cold run of 2026-10-17 its live searches took about 72 of the 105 minutes.
Step B looks up local files by the recordings MusicBrainz returns and keeps only files with the
broadcast artist's normalized name, so it can only add a PRESENT file of that name that carries
a recording_mbid and that Step A (files tagged with the artist MBID, as artist or album artist)
has not already found. A file Step A found cannot make Step B win: each file's score does not
depend on the other candidates, and Step B displaces Step A only with a strictly higher score.

So the strategy asks RecordingSearchReachRepository once per (artist name, artist MBID) per
run, and calls MusicBrainz only when such a file exists. Outcomes are unchanged. Proposed
ruling AUD-R031: during an outage a song Step B cannot help is decided as usual instead of
deferred as MB_UNAVAILABLE (AUD-R028, D16).

Candidate scores are pinned per file by patching _candidate_scores, as test_mb_outage_songs.py
does, so the tests do not depend on title scoring.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, ReasonCode
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
from backend.services.title_scoring import broadcast_title_variants
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.log_name_links import FakeLogNameLinkRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient
from tests.fakes.recording_search_reach import FakeRecordingSearchReach
from tests.matching_seed import seed_link

Scores = dict[UUID, float]
Outcome = tuple[
    MatchStatus,
    MatchTier | None,
    ReasonCode | None,
    str | None,
    list[tuple[UUID | None, float, MatchTier | None]],
]


@pytest.fixture
def scores(monkeypatch: pytest.MonkeyPatch) -> Scores:
    """Pin each candidate's score by library-file id."""
    table: Scores = {}

    def pinned(
        full_bcs: list[str], core_bcs: list[str], f: LibraryFile, threshold: int
    ) -> tuple[float, float]:
        return table[f.id], table[f.id]

    monkeypatch.setattr("backend.services.identity_matching_service._candidate_scores", pinned)
    return table


class _Mb(FakeMbClient):
    """FakeMbClient whose recording search can be down (an outage)."""

    def __init__(
        self,
        recording_searches: dict[tuple[str, str], list[dict[str, Any]]],
        *,
        down: BaseException | None = None,
    ) -> None:
        super().__init__(recording_searches=recording_searches)
        self.down = down

    def search_recording(
        self, artist_mbid: str, title: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        found = super().search_recording(artist_mbid, title, limit)
        if self.down is not None:
            raise self.down
        return found

    @property
    def searches(self) -> int:
        return sum(1 for call in self.calls if call.startswith("search_recording:"))


class _World:
    """One playlist. Every artist is auto_matched, linked to a stub of the MBID
    ``mbid-<name>`` (no catalog artist holds it): its songs go through Steps A, B and C."""

    def __init__(self, scores: Scores) -> None:
        self.scores = scores
        self.playlist_id = uuid4()
        self.artists = FakeBroadcastArtistRepository()
        self.songs = FakeBroadcastTrackIdentityRepository()
        self.matches = FakeMatchRepository()
        self.links = FakeLogNameLinkRepository()
        self.catalog = FakeArtistRepository()
        self.files = FakeLibraryFileRepository()
        self.reach = FakeRecordingSearchReach(self.files)
        self.recording_searches: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._by_name: dict[str, BroadcastArtist] = {}

    @staticmethod
    def mbid(name: str) -> str:
        return f"mbid-{name}"

    def artist(self, name: str) -> BroadcastArtist:
        if name in self._by_name:
            return self._by_name[name]
        artist = BroadcastArtist(
            id=uuid4(),
            original_name=name,
            normalized_name=normalize_artist(name),
            match_status=MatchStatus.AUTO_MATCHED,
        )
        self.artists.upsert(artist)
        seed_link(self.links, self.catalog, artist.id, self.mbid(name))
        self._by_name[name] = artist
        return artist

    def song(self, artist_name: str, title: str) -> BroadcastTrackIdentity:
        artist = self.artist(artist_name)
        norm = normalize_title(title)
        song = self.songs.upsert(
            BroadcastTrackIdentity(
                id=uuid4(),
                broadcast_artist_id=artist.id,
                original_title=title,
                normalized_title=norm,
                normalized_signature=compute_normalized_signature(artist.normalized_name, norm),
            )
        )
        self.songs.register_playlist_identity(self.playlist_id, song.id)
        return song

    def file(
        self,
        artist_name: str,
        score: float,
        *,
        recording_mbid: str | None = None,
        tagged: bool = False,
        album_tagged: bool = False,
        artist_mbid: str | None = None,
        missing: bool = False,
        found_by_search_for: str | None = None,
    ) -> LibraryFile:
        """A file named for the artist, scoring ``score`` against any title.

        ``tagged`` / ``album_tagged`` carry the artist's MBID as artist_mbid /
        album_artist_mbid (a Step A candidate). ``found_by_search_for`` makes MusicBrainz
        return the file's recording when the artist's MBID is searched with that title.
        """
        path = f"/music/{uuid4().hex}.flac"
        mbid = self.mbid(artist_name)
        library_file = LibraryFile(
            id=uuid4(),
            file_path=path,
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
            audio=AudioMetadata(
                recording_mbid=recording_mbid,
                artist_mbid=mbid if tagged else artist_mbid,
                album_artist_mbid=mbid if album_tagged else None,
                artist_name=artist_name,
                normalized_artist_name=normalize_artist(artist_name),
            ),
        )
        self.files.upsert(library_file)
        if missing:
            self.files.mark_missing(path)
        self.scores[library_file.id] = score
        if found_by_search_for is not None:
            assert recording_mbid is not None
            self.answer(artist_name, found_by_search_for, recording_mbid)
        return library_file

    def answer(self, artist_name: str, title: str, recording_mbid: str) -> None:
        """MusicBrainz returns this recording when the artist's MBID is searched with this
        broadcast title (Step B searches with its first title variant)."""
        key = (self.mbid(artist_name), broadcast_title_variants(title)[0])
        self.recording_searches.setdefault(key, []).append({"id": recording_mbid})

    def run(self, *, with_reach: bool = True, down: BaseException | None = None) -> _Mb:
        mb = _Mb(self.recording_searches, down=down)
        repos = IdentityMatchingRepos(
            track_identity_repo=self.songs,
            broadcast_artist_repo=self.artists,
            match_repo=self.matches,
            library_file_repo=self.files,
            rules_repo=FakeMappingRuleRepository(),
            catalog_repo=self.catalog,
            link_repo=self.links,
            **({"recording_search_reach": self.reach} if with_reach else {}),
        )
        match_identities_for_playlist(self.playlist_id, repos, mb)
        return mb

    def comparable(self, song: BroadcastTrackIdentity) -> tuple[object, ...]:
        """The outcome with each row's file named by its pinned score: file ids differ
        between two worlds built alike."""
        *decision, rows = self.outcome(song)
        return (*decision, [(score, tier) for _f, score, tier in rows])

    def outcome(self, song: BroadcastTrackIdentity) -> Outcome:
        stored = self.songs.get_by_id(song.id)
        assert stored is not None
        rows = sorted(
            (
                (m.library_file_id, m.confidence_score, m.match_tier)
                for m in self.matches._data.values()
                if m.identity_id == song.id
            ),
            key=str,
        )
        return (
            stored.match_status,
            stored.match_tier,
            stored.reason_code,
            stored.reason_detail,
            rows,
        )


# --- Songs Step B cannot help: no search ---------------------------------------------------------


NO_REACH: dict[str, Callable[[_World], object]] = {
    "no_files": lambda w: None,
    "untagged_file": lambda w: w.file("Blur", 70.0),
    "artist_tagged_recording": lambda w: w.file("Blur", 70.0, recording_mbid="r1", tagged=True),
    "album_artist_tagged_recording": lambda w: w.file(
        "Blur", 70.0, recording_mbid="r1", album_tagged=True
    ),
    "missing_recording": lambda w: w.file("Blur", 70.0, recording_mbid="r1", missing=True),
    "another_artists_recording": lambda w: w.file("Oasis", 70.0, recording_mbid="r1"),
}


@pytest.mark.parametrize("setup", list(NO_REACH.values()), ids=list(NO_REACH))
def test_no_search_when_the_artist_has_no_file_step_b_could_add(
    scores: Scores, setup: Callable[[_World], object]
) -> None:
    worlds = []
    for with_reach in (True, False):
        world = _World(scores)
        setup(world)
        song = world.song("Blur", "Song 2")
        mb = world.run(with_reach=with_reach)
        worlds.append((world.comparable(song), mb.searches))

    (gated, gated_searches), (ungated, ungated_searches) = worlds
    assert gated_searches == 0
    assert ungated_searches == 1
    assert gated == ungated  # the search could not change the song


# --- Songs Step B can help: the search still runs -------------------------------------------------


def test_step_b_still_finds_a_recording_tagged_file_off_the_artist_mbid(scores: Scores) -> None:
    world = _World(scores)
    found = world.file("Blur", 100.0, recording_mbid="r1", found_by_search_for="Song 2")
    song = world.song("Blur", "Song 2")

    mb = world.run()

    assert mb.searches == 1
    status, tier, _rc, _rd, rows = world.outcome(song)
    assert (status, tier) == (MatchStatus.AUTO_MATCHED, MatchTier.MUSICBRAINZ_ID_SEARCH)
    assert rows == [(found.id, 100.0, MatchTier.MUSICBRAINZ_ID_SEARCH)]


def test_step_b_still_displaces_a_weaker_step_a(scores: Scores) -> None:
    world = _World(scores)
    world.file("Blur", 70.0, recording_mbid="r0", tagged=True)
    better = world.file("Blur", 75.0, recording_mbid="r1", found_by_search_for="Song 2")
    song = world.song("Blur", "Song 2")

    mb = world.run()

    assert mb.searches == 1
    _status, tier, _rc, _rd, rows = world.outcome(song)
    assert tier == MatchTier.MUSICBRAINZ_ID_SEARCH
    assert rows == [(better.id, 75.0, MatchTier.MUSICBRAINZ_ID_SEARCH)]


# --- Once per artist per run ----------------------------------------------------------------------


def test_reach_is_asked_once_per_artist_per_run(scores: Scores) -> None:
    world = _World(scores)
    world.file("Blur", 70.0)
    world.file("Oasis", 70.0, recording_mbid="r1")
    for title in ("Song 2", "Beetlebum", "Parklife"):
        world.song("Blur", title)
    for title in ("Wonderwall", "Live Forever"):
        world.song("Oasis", title)

    mb = world.run()

    assert sorted(world.reach.queries) == [
        (normalize_artist("Blur"), world.mbid("Blur")),
        (normalize_artist("Oasis"), world.mbid("Oasis")),
    ]
    assert mb.searches == 2  # Oasis's two songs; none of Blur's


def test_without_the_reach_port_step_b_always_runs(scores: Scores) -> None:
    world = _World(scores)
    world.file("Blur", 70.0)
    world.song("Blur", "Song 2")
    world.song("Blur", "Parklife")

    mb = world.run(with_reach=False)

    assert mb.searches == 2
    assert world.reach.queries == []


# --- An outage (proposed AUD-R031) ----------------------------------------------------------------


def test_an_outage_defers_only_songs_step_b_could_help(scores: Scores) -> None:
    world = _World(scores)
    local_only = world.file("Blur", 100.0)  # Step C auto-matches it
    world.file("Oasis", 100.0, recording_mbid="r1")
    blur = world.song("Blur", "Song 2")
    oasis = world.song("Oasis", "Wonderwall")

    mb = world.run(down=httpx.ConnectError("connection refused"))

    assert mb.searches == 1
    assert world.outcome(blur) == (
        MatchStatus.AUTO_MATCHED,
        MatchTier.LOCAL_FILE_FUZZY,
        None,
        None,
        [(local_only.id, 100.0, MatchTier.LOCAL_FILE_FUZZY)],
    )
    assert world.outcome(oasis)[:3] == (
        MatchStatus.NEEDS_REVIEW,
        MatchTier.UNCLASSIFIED,
        ReasonCode.MB_UNAVAILABLE,
    )


# --- Outcomes are unchanged -----------------------------------------------------------------------


def _mixed_world(world: _World) -> list[BroadcastTrackIdentity]:
    """Artists of every kind, with MusicBrainz answering for every song it could."""
    # Step A only, with MusicBrainz returning Step A's own files (B finds nothing new).
    world.file("Blur", 70.0, recording_mbid="b1", tagged=True, found_by_search_for="Song 2")
    world.file("Blur", 50.0, recording_mbid="b2", album_tagged=True, found_by_search_for="Tender")
    # Untagged files only: Step C decides.
    world.file("Pulp", 90.0)
    world.file("Pulp", 60.0)
    # A recording-tagged file off the artist MBID beats Step A.
    world.file("Oasis", 60.0, recording_mbid="o1", tagged=True)
    world.file("Oasis", 85.0, recording_mbid="o2", found_by_search_for="Wonderwall")
    # A missing recording-tagged file: Step B could only have found it if present.
    world.file("Suede", 95.0, recording_mbid="s1", missing=True, found_by_search_for="Animal")
    world.file("Suede", 40.0)
    # Step B wins outright for Elastica; for Verve, MusicBrainz returns Elastica's file, which
    # _eligible drops.
    world.file("Elastica", 99.0, recording_mbid="e1", found_by_search_for="Connection")
    world.file("Verve", 30.0, recording_mbid="v1", artist_mbid="mbid-other")
    world.answer("Verve", "Connection", "e1")
    return [
        world.song("Blur", "Song 2"),
        world.song("Blur", "Tender"),
        world.song("Blur", "Parklife"),
        world.song("Pulp", "Common People"),
        world.song("Oasis", "Wonderwall"),
        world.song("Oasis", "Live Forever"),
        world.song("Suede", "Animal"),
        world.song("Verve", "Connection"),
        world.song("Verve", "Bitter Sweet Symphony"),
        world.song("Elastica", "Connection"),
    ]


def test_outcomes_are_unchanged_and_searches_drop(scores: Scores) -> None:
    gated_world, ungated_world = _World(scores), _World(scores)
    gated_songs, ungated_songs = _mixed_world(gated_world), _mixed_world(ungated_world)

    gated_mb = gated_world.run()
    ungated_mb = ungated_world.run(with_reach=False)

    gated = [gated_world.comparable(s) for s in gated_songs]
    assert gated == [ungated_world.comparable(s) for s in ungated_songs]
    # Step B decided some songs, so the comparison covers its wins, not only its misses.
    assert sum(1 for o in gated if o[1] == MatchTier.MUSICBRAINZ_ID_SEARCH) >= 2
    assert ungated_mb.searches == 10
    assert gated_mb.searches == 5  # Oasis's two songs, Verve's two and Elastica's one
