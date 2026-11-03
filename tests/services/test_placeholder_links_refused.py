"""Placeholders are never linked (artist model phase 1c, SPEC S5, S6, E3; C7, AUD-R037).

S5: the link writer (``LogNameLinkService``) refuses every link to one of MusicBrainz's
special-purpose artists (``SPECIAL_PURPOSE_MBIDS``: Various Artists, [unknown] and the rest),
whether the target names the MBID or the catalog artist holding it. A refused automatic link
writes nothing, not even the stub an unknown MBID would get or the catalog upsert of a
MusicBrainz hit; the plan of a matcher answer (``plan_answer``) leaves a placeholder out, and a
link by hand is refused with ``PlaceholderArtistError`` (the Resolve route answers 422).
Migration 0045 deleted the links that existed
(``test_link_demotion_pg.py::test_placeholder_links_removed``).

E3, the two halves PR 1 left (PR 1 proved the rejected pair, ``test_interim_artist_rules.py``):

- the matcher proposes a placeholder artist: an exact name, a fuzzy score of 95 or more, or a
  MusicBrainz hit. Nothing is written for it: no link, no suggestion, no catalog upsert. A
  suggestion of one is dropped and the others stay; when nothing is left the name goes to
  ``needs_review`` with ``NO_CANDIDATES`` and a detail, and starts no cascade;
- the matcher meets a name on the not-an-artist list (``log_name_placeholders``, S6). The name
  is never matched: no engine answers for it (no MusicBrainz search either), no link is
  written, and it goes to ``needs_review`` with ``NO_CANDIDATES`` and no cascade, so its songs
  stay as they were. The list holds exact cleaned names only ("38 Special" is a real act).

Scores are rapidfuzz ``token_sort_ratio`` of the normalized names, as the matcher computes them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.catalog import Artist
from backend.domain.enums import (
    CatalogSource,
    LogNameLinkState,
    MatchStatus,
    MatchTier,
    ReasonCode,
)
from backend.domain.log_name_status import NameState
from backend.domain.matching import PlaceholderArtistError
from backend.services.artist_linking_service import SPECIAL_PURPOSE_MBIDS
from backend.services.artist_matching_service import (
    ArtistMatchingRepos,
    match_artists_for_playlist,
)
from backend.services.log_name_link_service import LinkTarget, LogNameLinkService, MatcherAnswer
from backend.services.mb_types import MbArtistResult
from backend.services.normalization import compute_normalized_signature, normalize_artist
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.log_name_links import FakeLogNameLinkRepository
from tests.fakes.log_name_placeholders import FakeLogNamePlaceholderRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient

VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"
UNKNOWN = "125ec42a-7229-4250-afc5-e057484327fe"
FILLERS = ("Metallica", "Radiohead")  # each scores under 56 against every log name here
# A name cut off at 30 characters: the MusicBrainz tier runs for it. Nothing local scores 56.
TRUNCATED = "WANDERING PRAIRIE SKYLINE WAND"

PENDING_SONG = (MatchStatus.PENDING, None)
REFUSED = (MatchStatus.NEEDS_REVIEW, ReasonCode.NO_CANDIDATES)
SUGGESTED = (MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_SUGGESTED)


def _artist(name: str, mbid: str | None) -> Artist:
    return Artist(
        id=str(uuid4()),
        name=name,
        sort_name=name,
        normalized_name=normalize_artist(name),
        mbid=mbid,
        origin=CatalogSource.MUSICBRAINZ if mbid else CatalogSource.LOCAL,
        needs_enhancement=False,
    )


def _log_name(name: str) -> BroadcastArtist:
    return BroadcastArtist(id=uuid4(), original_name=name, normalized_name=normalize_artist(name))


# --- S5: the writer ------------------------------------------------------------------------------


@dataclass
class _Writer:
    names: FakeBroadcastArtistRepository
    catalog: FakeArtistRepository
    links: FakeLogNameLinkRepository
    service: LogNameLinkService

    def name(self, spelling: str = "VARIOUS ARTISTS") -> UUID:
        return self.names.upsert(_log_name(spelling)).id


def _writer() -> _Writer:
    names = FakeBroadcastArtistRepository()
    catalog = FakeArtistRepository()
    links = FakeLogNameLinkRepository(artists=catalog, names=names)
    return _Writer(names, catalog, links, LogNameLinkService(links, catalog, names))


@pytest.mark.parametrize("mbid", sorted(SPECIAL_PURPOSE_MBIDS))
def test_no_automatic_link_to_a_placeholder_mbid(mbid: str) -> None:
    """Every special-purpose MBID is refused, and no stub is made for it."""
    writer = _writer()
    name_id = writer.name()

    writer.service.link_automatically(name_id, mbid, MatchTier.NORMALIZATION, 100.0)

    assert writer.links.list_by_name(name_id) == []
    assert writer.catalog.get_by_mbid(mbid) is None


def test_no_automatic_link_to_the_catalog_artist_holding_one() -> None:
    writer = _writer()
    various = writer.catalog.upsert(_artist("Various Artists", VARIOUS_ARTISTS))
    name_id = writer.name()

    writer.service.link_automatically(name_id, various.id, MatchTier.NORMALIZATION, 100.0)
    writer.service.link_automatically(name_id, VARIOUS_ARTISTS, MatchTier.MUSICBRAINZ_API, 97.0)

    assert writer.links.list_by_name(name_id) == []


@pytest.mark.parametrize("by_id", [False, True], ids=["by MBID", "by artist id"])
def test_a_placeholder_is_never_linked_by_hand(by_id: bool) -> None:
    """The curator's link is refused before anything is written: the name keeps its status and
    gains no link."""
    writer = _writer()
    unknown = writer.catalog.upsert(_artist("[unknown]", UNKNOWN))
    name_id = writer.name("UNKNOWN")
    writer.names.update_match_status(name_id, MatchStatus.NEEDS_REVIEW, ReasonCode.LOW_CONFIDENCE)

    with pytest.raises(PlaceholderArtistError):
        writer.service.link_by_hand(name_id, unknown.id if by_id else UNKNOWN)

    stored = writer.names.get_by_id(name_id)
    assert stored is not None
    assert (stored.match_status, stored.reason_code) == (
        MatchStatus.NEEDS_REVIEW,
        ReasonCode.LOW_CONFIDENCE,
    )
    assert writer.links.list_by_name(name_id) == []


def test_a_placeholder_mbid_no_artist_holds_is_refused_by_hand_without_a_stub() -> None:
    writer = _writer()
    name_id = writer.name()

    with pytest.raises(PlaceholderArtistError):
        writer.service.link_by_hand(name_id, VARIOUS_ARTISTS)

    assert writer.catalog.get_by_mbid(VARIOUS_ARTISTS) is None
    assert writer.links.list_by_name(name_id) == []


def test_the_writer_plans_no_placeholder_suggestion() -> None:
    """A matcher answer's plan (``plan_answer``, then ``write_plan``) leaves out a placeholder
    suggestion, local or MusicBrainz, and writes the others; the placeholder's hit is not
    upserted."""
    writer = _writer()
    various = writer.catalog.upsert(_artist("Various Artists", VARIOUS_ARTISTS))
    band = writer.catalog.upsert(_artist("Various Artistry Band", str(uuid4())))
    name_id = writer.name("VARIOUS ARTISTRY")
    unknown_hit: MbArtistResult = {"id": UNKNOWN, "name": "[unknown]", "score": 85}
    answer = MatcherAnswer(
        NameState(MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_SUGGESTED, "3 suggested artists"),
        suggestions=(
            LinkTarget(various.id, MatchTier.NORMALIZATION, 90.32),
            LinkTarget(UNKNOWN, MatchTier.MUSICBRAINZ_API, 85.0, unknown_hit),
            LinkTarget(band.id, MatchTier.NORMALIZATION, 86.49),
        ),
    )

    plan = writer.service.plan_answer(name_id, answer)
    assert writer.service.write_plan(name_id, plan)

    assert [target.target for target, _ in plan.writes] == [band.id]
    assert {link.artist_id: link.state for link in writer.links.list_by_name(name_id)} == {
        band.id: LogNameLinkState.SUGGESTED
    }
    assert writer.catalog.get_by_mbid(UNKNOWN) is None
    assert writer.catalog.musicbrainz_upserts == []


@pytest.mark.parametrize(
    "link",
    [
        LinkTarget(VARIOUS_ARTISTS, MatchTier.NORMALIZATION, 100.0),
        LinkTarget(
            UNKNOWN,
            MatchTier.MUSICBRAINZ_API,
            100.0,
            {"id": UNKNOWN, "name": "[unknown]", "score": 100},
        ),
    ],
    ids=["local", "MusicBrainz"],
)
def test_the_writer_plans_no_placeholder_link(link: LinkTarget) -> None:
    """An automatic link to a placeholder is not planned, and the status the plan writes is the
    run's refusal, never a match with no link (S3's rule would not give it)."""
    writer = _writer()
    name_id = writer.name()
    answer = MatcherAnswer(NameState(MatchStatus.AUTO_MATCHED), link=link)

    plan = writer.service.plan_answer(name_id, answer)
    assert writer.service.write_plan(name_id, plan)

    assert plan.writes == ()
    assert (plan.state.status, plan.state.reason_code) == REFUSED
    assert writer.links.list_by_name(name_id) == []
    assert writer.catalog.get_by_mbid(link.target) is None
    assert writer.catalog.musicbrainz_upserts == []


# --- E3: a matcher run ---------------------------------------------------------------------------


@dataclass
class _World:
    names: FakeBroadcastArtistRepository
    songs: FakeBroadcastTrackIdentityRepository
    catalog: FakeArtistRepository
    links: FakeLogNameLinkRepository
    placeholders: FakeLogNamePlaceholderRepository
    playlist_id: UUID
    pending: Callable[[str], tuple[BroadcastArtist, UUID]]
    """A pending name played on the playlist, with one pending song; the name and the song."""

    def add(self, name: str, mbid: str | None = None) -> Artist:
        return self.catalog.upsert(_artist(name, mbid or str(uuid4())))

    def run(self, mb_client: FakeMbClient | None = None) -> None:
        match_artists_for_playlist(
            playlist_id=self.playlist_id,
            repos=ArtistMatchingRepos(
                broadcast_artist_repo=self.names,
                track_identity_repo=self.songs,
                artist_repo=self.catalog,
                match_repo=FakeMatchRepository(),
                rules_repo=FakeMappingRuleRepository(),
                link_repo=self.links,
                placeholders=self.placeholders,
            ),
            mb_client=mb_client or FakeMbClient(),
        )

    def name_state(self, name_id: UUID) -> tuple[MatchStatus, ReasonCode | None]:
        name = self.names.get_by_id(name_id)
        assert name is not None
        return name.match_status, name.reason_code

    def detail(self, name_id: UUID) -> str | None:
        name = self.names.get_by_id(name_id)
        assert name is not None
        return name.reason_detail

    def song_state(self, song_id: UUID) -> tuple[MatchStatus, ReasonCode | None]:
        song = self.songs.get_by_id(song_id)
        assert song is not None
        return song.match_status, song.reason_code

    def links_of(self, name_id: UUID) -> dict[str, tuple[str, str, int]]:
        return {
            link.artist_id: (link.state.value, link.origin.value, link.strength)
            for link in self.links.list_by_name(name_id)
        }


def _world(*listed: str) -> _World:
    """A world whose not-an-artist list holds *listed* (normalized names)."""
    names = FakeBroadcastArtistRepository()
    songs = FakeBroadcastTrackIdentityRepository()
    catalog = FakeArtistRepository()
    playlist_id = uuid4()

    def pending(spelling: str) -> tuple[BroadcastArtist, UUID]:
        name = names.upsert(_log_name(spelling))
        names.register_playlist_artist(playlist_id, name.id)
        song = songs.upsert(
            BroadcastTrackIdentity(
                id=uuid4(),
                broadcast_artist_id=name.id,
                original_title="Song",
                normalized_title="song",
                normalized_signature=compute_normalized_signature(name.normalized_name, "song"),
            )
        )
        songs.register_playlist_identity(playlist_id, song.id)
        return name, song.id

    world = _World(
        names=names,
        songs=songs,
        catalog=catalog,
        links=FakeLogNameLinkRepository(artists=catalog, names=names),
        placeholders=FakeLogNamePlaceholderRepository(listed),
        playlist_id=playlist_id,
        pending=pending,
    )
    for filler in FILLERS:
        world.add(filler)
    return world


def _refused_and_untouched(world: _World, name_id: UUID, song_id: UUID) -> None:
    assert world.name_state(name_id) == REFUSED
    assert world.detail(name_id)
    assert world.links_of(name_id) == {}
    assert world.song_state(song_id) == PENDING_SONG


@pytest.mark.parametrize(
    ("log_name", "score"),
    [("VARIOUS ARTISTS", 100.0), ("VARIOUS ARTIST", 96.55)],
    ids=["exact name", "fuzzy 95+"],
)
def test_a_match_to_a_placeholder_artist_is_not_written(log_name: str, score: float) -> None:
    """The matcher answers Various Artists (an exact name with one artist, or 95 or more):
    nothing is linked, and the name waits in review with no cascade."""
    world = _world()
    world.add("Various Artists", VARIOUS_ARTISTS)
    name, song = world.pending(log_name)

    world.run()

    _refused_and_untouched(world, name.id, song)


def test_a_placeholder_suggestion_is_dropped_and_the_others_stay() -> None:
    """VARIOUS ARTISTRY scores 90.32 against Various Artists and 86.49 against a real act: only
    the real act is suggested."""
    world = _world()
    various = world.add("Various Artists", VARIOUS_ARTISTS)
    band = world.add("Various Artistry Band")
    name, song = world.pending("VARIOUS ARTISTRY")

    world.run()

    assert world.name_state(name.id) == SUGGESTED
    assert world.links_of(name.id) == {band.id: ("suggested", "automatic", 86)}
    assert various.id not in world.links_of(name.id)
    assert world.song_state(song) == PENDING_SONG


def test_a_musicbrainz_placeholder_hit_is_neither_upserted_nor_linked() -> None:
    world = _world()
    name, song = world.pending(TRUNCATED)
    hits = [{"id": VARIOUS_ARTISTS, "name": "Various Artists", "score": 100}]

    world.run(FakeMbClient(responses={TRUNCATED: hits}))

    _refused_and_untouched(world, name.id, song)
    assert world.catalog.get_by_mbid(VARIOUS_ARTISTS) is None
    assert world.catalog.musicbrainz_upserts == []


def test_a_musicbrainz_placeholder_suggestion_is_dropped_and_not_upserted() -> None:
    world = _world()
    name, song = world.pending(TRUNCATED)
    hits = [
        {"id": UNKNOWN, "name": "[unknown]", "score": 85},
        {"id": "mb-real", "name": "Wandering Prairie", "score": 78},
    ]

    world.run(FakeMbClient(responses={TRUNCATED: hits}))

    assert world.name_state(name.id) == SUGGESTED
    real = world.catalog.get_by_mbid("mb-real")
    assert real is not None
    assert world.links_of(name.id) == {real.id: ("suggested", "automatic", 78)}
    assert world.catalog.get_by_mbid(UNKNOWN) is None
    assert world.song_state(song) == PENDING_SONG


def test_a_listed_name_is_never_matched() -> None:
    """SPOT BREAK is on the list, and the catalog holds an artist of that very name: the
    matcher writes no link and no suggestion, and the name waits in review."""
    world = _world("spot break")
    world.add("Spot Break")
    name, song = world.pending("SPOT BREAK")

    world.run()

    _refused_and_untouched(world, name.id, song)


def test_a_listed_name_starts_no_cascade() -> None:
    """A listed name the matcher would have deferred (nothing scores 56): its songs are not
    deferred (the DEFERRED_RETRY cascade would park them)."""
    world = _world("syndicated program")
    name, song = world.pending("SYNDICATED PROGRAM")

    world.run()

    _refused_and_untouched(world, name.id, song)


def test_a_listed_name_is_not_searched_on_musicbrainz() -> None:
    """A listed name that looks cut off: no MusicBrainz search is made for it, and a hit it
    would have had is not written."""
    world = _world(normalize_artist(TRUNCATED))
    name, song = world.pending(TRUNCATED)
    client = FakeMbClient(responses={TRUNCATED: [{"id": "mb-a", "name": "A", "score": 100}]})

    world.run(client)

    _refused_and_untouched(world, name.id, song)
    assert client.calls == []
    assert world.catalog.musicbrainz_upserts == []


def test_the_list_holds_exact_names_only() -> None:
    """With the five station names listed, 38 SPECIAL and SPOT BREAKS are matched as before."""
    world = _world(
        "spot break", "special programming", "syndicated program", "various", "various artists"
    )
    special = world.add("38 Special")
    breaks = world.add("Spot Breaks")
    name, _ = world.pending("38 SPECIAL")
    plural, _ = world.pending("SPOT BREAKS")

    world.run()

    assert world.name_state(name.id) == (MatchStatus.AUTO_MATCHED, None)
    assert world.links_of(name.id) == {special.id: ("active", "automatic", 100)}
    assert world.name_state(plural.id) == (MatchStatus.AUTO_MATCHED, None)
    assert world.links_of(plural.id) == {breaks.id: ("active", "automatic", 100)}


def test_other_names_of_the_run_are_matched_as_before() -> None:
    """One listed name in a run does not stop the others."""
    world = _world("spot break")
    metallica = world.catalog.get_by_normalized_name("metallica")
    assert metallica is not None
    listed, listed_song = world.pending("SPOT BREAK")
    other, _ = world.pending("METALLICA")

    world.run()

    _refused_and_untouched(world, listed.id, listed_song)
    assert world.name_state(other.id) == (MatchStatus.AUTO_MATCHED, None)
    assert world.links_of(other.id) == {metallica.id: ("active", "automatic", 100)}
