"""Tier 1 reads a log name's link from the link service (artist model phase 1a, SPEC S7; C30,
AUD-R035).

Tier 1 (``ResolvedArtistMbidStrategy``) uses the name's first active link, ordered manual
first, then strength (high to low), then ``created_at`` (oldest first); suggested and rejected
links are never used. The MBID it searches with is the linked artist's current
``artists.mbid``, read through the foreign key, never the MBID copied onto the link; a
local-only artist has none, so Steps A and B are skipped and Step C matches by name. A
resolved name with no active link is a data fault: ``MISSING_MATCH_RECORD``, as before 1a.
Steps A, B and C run as today.

Each name gets one song, "Poison", and each candidate artist a file tagged with its MBID under
the name's spelling, so the file the song matches shows which link Tier 1 used.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.catalog import Artist
from backend.domain.enums import (
    EnrichmentStatus,
    LogNameLinkOrigin,
    LogNameLinkState,
    MatchStatus,
    MatchTier,
    ReasonCode,
)
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import LogNameLink
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
from tests.fakes.log_name_links import FakeLogNameLinkRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
TITLE = "poison"


@dataclass
class _World:
    names: FakeBroadcastArtistRepository = field(default_factory=FakeBroadcastArtistRepository)
    songs: FakeBroadcastTrackIdentityRepository = field(
        default_factory=FakeBroadcastTrackIdentityRepository
    )
    catalog: FakeArtistRepository = field(default_factory=FakeArtistRepository)
    files: FakeLibraryFileRepository = field(default_factory=FakeLibraryFileRepository)
    matches: FakeMatchRepository = field(default_factory=FakeMatchRepository)
    mb: FakeMbClient = field(default_factory=FakeMbClient)
    playlist_id: UUID = field(default_factory=uuid4)
    links: FakeLogNameLinkRepository = field(init=False)

    def __post_init__(self) -> None:
        self.links = FakeLogNameLinkRepository(artists=self.catalog, names=self.names)

    def name(self, spelling: str, status: MatchStatus = MatchStatus.AUTO_MATCHED) -> UUID:
        artist = self.names.upsert(
            BroadcastArtist(
                id=uuid4(),
                original_name=spelling,
                normalized_name=normalize_artist(spelling),
                match_status=status,
            )
        )
        song = BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title=TITLE,
            normalized_title=normalize_title(TITLE),
            normalized_signature=compute_normalized_signature(artist.normalized_name, TITLE),
        )
        self.songs.upsert(song)
        self.songs.register_playlist_identity(self.playlist_id, song.id)
        return artist.id

    def artist(self, label: str, mbid: str | None) -> Artist:
        return self.catalog.upsert(
            Artist(id=f"artist-{label}", name=label, sort_name=label, mbid=mbid)
        )

    def link(
        self,
        name_id: UUID,
        artist: Artist,
        *,
        origin: LogNameLinkOrigin = LogNameLinkOrigin.AUTOMATIC,
        state: LogNameLinkState = LogNameLinkState.ACTIVE,
        strength: int = 100,
        at: datetime = T0,
    ) -> None:
        self.links.upsert_link(
            LogNameLink(
                broadcast_artist_id=name_id,
                artist_id=artist.id,
                mbid=artist.mbid,
                state=state,
                origin=origin,
                strength=strength,
                evidence={"version": 0},
                created_at=at,
            )
        )

    def file(self, name_id: UUID, mbid: str | None) -> UUID:
        """A file of "Poison" under the name's spelling, tagged with *mbid*."""
        name = self.names.get_by_id(name_id)
        assert name is not None
        file = LibraryFile(
            id=uuid4(),
            file_path=f"/music/{uuid4().hex}.flac",
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
            work_id=f"work-{uuid4().hex[:8]}",
            audio=AudioMetadata(
                artist_mbid=mbid,
                track_title=TITLE,
                normalized_title=normalize_title(TITLE),
                artist_name=name.original_name,
                normalized_artist_name=name.normalized_name,
            ),
        )
        self.files.upsert(file)
        return file.id

    def run(self) -> None:
        match_identities_for_playlist(
            playlist_id=self.playlist_id,
            repos=IdentityMatchingRepos(
                track_identity_repo=self.songs,
                broadcast_artist_repo=self.names,
                match_repo=self.matches,
                library_file_repo=self.files,
                rules_repo=FakeMappingRuleRepository(),
                catalog_repo=self.catalog,
                link_repo=self.links,
            ),
            mb_client=self.mb,
        )

    def matched(self, name_id: UUID) -> tuple[UUID | None, MatchTier | None]:
        """The file the name's song matched, and the tier."""
        [song] = self.songs.get_for_artist(name_id)
        match = self.matches.get_by_identity(song.id)
        stored = self.songs.get_by_id(song.id)
        assert stored is not None
        return (match.library_file_id if match else None), stored.match_tier


def test_link_order_and_live_mbid() -> None:
    world = _World()
    a, b, c, d, e = (world.artist(x, f"mbid-{x}") for x in "abcde")

    manual_first = world.name("MANUAL FIRST", MatchStatus.MANUAL_MATCHED)
    world.link(manual_first, a, strength=100, at=T0 - HOUR)
    world.link(manual_first, b, origin=LogNameLinkOrigin.MANUAL, strength=40, at=T0 + HOUR)
    world.link(manual_first, c, origin=LogNameLinkOrigin.LEGACY, strength=100, at=T0)
    world.link(manual_first, d, origin=LogNameLinkOrigin.MANUAL, state=LogNameLinkState.SUGGESTED)
    world.link(manual_first, e, origin=LogNameLinkOrigin.MANUAL, state=LogNameLinkState.REJECTED)
    strongest = world.name("STRONGEST")
    world.link(strongest, a, strength=70, at=T0 - HOUR)
    world.link(strongest, b, strength=90, at=T0 + HOUR)
    oldest = world.name("OLDEST")
    world.link(oldest, a, strength=80, at=T0 + HOUR)
    world.link(oldest, b, strength=80, at=T0)
    expected = {manual_first: b, strongest: b, oldest: b}
    files = {
        (name_id, artist.id): world.file(name_id, artist.mbid)
        for name_id in expected
        for artist in (a, b, c, d, e)
    }

    # The live MBID: "f" was linked while local (no MBID on the link), and "g" has a new MBID
    # since its link copied the old one.
    gained = world.name("GAINED AN MBID")
    f = world.artist("f", None)
    world.link(gained, f)
    world.catalog.upsert(replace(f, mbid="mbid-f"))
    gained_file = world.file(gained, "mbid-f")
    moved = world.name("MOVED MBID")
    g = world.artist("g", "mbid-g-old")
    world.link(moved, g)
    world.catalog.upsert(replace(g, mbid="mbid-g-new"))
    world.file(moved, "mbid-g-old")
    moved_file = world.file(moved, "mbid-g-new")

    world.run()

    for name_id, artist in expected.items():
        assert world.matched(name_id) == (
            files[name_id, artist.id],
            MatchTier.MUSICBRAINZ_ID_EXACT,
        ), name_id
    assert world.matched(gained) == (gained_file, MatchTier.MUSICBRAINZ_ID_EXACT)
    assert world.matched(moved) == (moved_file, MatchTier.MUSICBRAINZ_ID_EXACT)


def test_a_local_only_artist_matches_by_name_without_musicbrainz() -> None:
    world = _World()
    local = world.name("LOCAL BAND")
    world.link(local, world.artist("local", None))
    by_name = world.file(local, None)

    world.run()

    assert world.matched(local) == (by_name, MatchTier.LOCAL_FILE_FUZZY)
    assert [call for call in world.mb.calls if call.startswith("search_recording")] == []


def test_a_resolved_name_without_an_active_link_is_a_data_fault() -> None:
    world = _World()
    no_active = world.name("NO ACTIVE LINK")
    world.link(no_active, world.artist("s", "mbid-s"), state=LogNameLinkState.SUGGESTED)
    world.link(no_active, world.artist("r", "mbid-r"), state=LogNameLinkState.REJECTED)
    world.file(no_active, "mbid-s")
    no_link = world.name("NO LINK AT ALL")

    world.run()

    for name_id in (no_active, no_link):
        [song] = world.songs.get_for_artist(name_id)
        stored = world.songs.get_by_id(song.id)
        assert stored is not None
        assert (stored.match_status, stored.match_tier, stored.reason_code) == (
            MatchStatus.NEEDS_REVIEW,
            MatchTier.MUSICBRAINZ_ID_SEARCH,
            ReasonCode.MISSING_MATCH_RECORD,
        )
        assert world.matches.get_by_identity(song.id) is None
