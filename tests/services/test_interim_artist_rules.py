"""The interim artist matcher (artist model phase 1c, SPEC S4, E3, E7; C35, AUD-R038).

Between phases 1 and 4 the old artist matcher links a log name automatically only on a score of
95 or more (``MB_AUTO_LINK_SCORE``), or on an exact name that points at exactly one artist.
Everything else it decided before becomes a suggestion:

- its two lead rules (80+ with a 10-point lead; 55 to 64 with a 5-point lead), through which
  #216's dry run auto-matched six names to named stubs wrongly (E7);
- an exact name that points at several artists: several catalog artists with an MBID whose name
  normalizes to the log name (the exact pass took the first one);
- a result that ended ``AMBIGUOUS_GAP`` or ``LOW_CONFIDENCE``.

A suggestion is a ``suggested`` link: origin ``automatic``, strength the score, evidence v0 with
the tier and the score. One answer gives at most 3, best first, each 56 or over. The name goes
to ``needs_review`` with ``LINK_SUGGESTED`` and no cascade: its songs stay as they were (the
DEFERRED_RETRY and AUTO_REJECTED cascades would stop by-name song matching for the name).
``MIN_PRESENTATION_SCORE`` rises from 50 to 56, as songs. A MusicBrainz suggestion upserts its
artist into the catalog first, as a MusicBrainz match does, so the panel can name it.
DEFERRED_RETRY is unchanged until phase 4.

E3, its rejected-pair half: the writer never suggests an artist the curator rejected for the
name (C19). When every suggestion of an answer is such a pair, the name goes to review with
``LINK_REJECTED`` (1b), still with no cascade. The placeholder-MBID and listed-name halves of E3
come with PR 2, which adds the placeholder refusals.

Scores are rapidfuzz ``token_sort_ratio`` of the normalized names, as the matcher computes them.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from backend.config import Settings
from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.catalog import Artist
from backend.domain.enums import (
    CatalogSource,
    LogNameLinkOrigin,
    LogNameLinkState,
    MatchStatus,
    MatchTier,
    ReasonCode,
)
from backend.services import matching_constants as mc
from backend.services.artist_matching_service import (
    ArtistMatchingRepos,
    ArtistMatchResult,
    ArtistMatchThresholds,
    MusicBrainzApiStrategy,
    NormalizationStrategy,
    _decide_artist_zone,
    match_artists_for_playlist,
)
from backend.services.mb_types import MbArtistResult
from backend.services.normalization import compute_normalized_signature, normalize_artist
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.log_name_links import FakeLogNameLinkRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient
from tests.matching_seed import seed_link

# Named stubs of #216's dry run (E7): each log name, the stub it was auto-matched to, and its
# score against the stub. Every other artist of the test's catalog scores under 56 against it.
E7_MID_BAND = [  # 55 to 64 with a 5-point lead
    ("ANTI-FLAG", "Artifas", 62.5),
    ("AQUEDUCT", "The Audacity", 62.5),
    ("WALLS OF JERICHO", "Eric Gales", 61.54),
    ("INCREDIBLE MOSES LEROY", "The Legendary Invisible Men", 62.22),
    ("BOB KUBAN & IN-MEN", "Bob and Doug McKenzie", 63.41),
]
E7_STRONG = [("MACEO PARKER", "Col. Parker", 81.82)]  # 80+ with a 10-point lead
E7 = E7_MID_BAND + E7_STRONG

# Against "BLUE OYSTER CULT": three fuzzy candidates of 56 or more and two that are not.
BLUE_OYSTER = [
    ("Blue Oyster Club", 87.5),
    ("Blue Oyster", 81.48),
    ("Bluesy Oysters", 73.33),
    ("Blue Cult", 72.0),  # the fourth: one answer suggests at most 3
    ("Blue Rodeo", 53.85),  # under the floor of 56
]
FILLERS = ("Metallica", "Radiohead")  # each scores under 56 against every log name here
# A name cut off at 30 characters: the MusicBrainz tier runs for it. Nothing local scores 56.
TRUNCATED = "WANDERING PRAIRIE SKYLINE WAND"


def _mbid() -> str:
    return str(uuid4())


def _strength(score: float) -> int:
    """A score as a link's strength: rounded half up, as the link service and 0044 do."""
    return math.floor(score + 0.5)


def _artist(name: str, mbid: str | None, *, normalized: bool = True) -> Artist:
    """A catalog artist. A second artist of a name a first one holds keeps no normalized name,
    as the catalog leaves it (enrichment's clash rule); the matcher reads the name itself."""
    return Artist(
        id=str(uuid4()),
        name=name,
        sort_name=name,
        normalized_name=normalize_artist(name) if normalized else None,
        mbid=mbid,
        origin=CatalogSource.MUSICBRAINZ if mbid else CatalogSource.LOCAL,
        needs_enhancement=False,
    )


def _log_name(name: str) -> BroadcastArtist:
    return BroadcastArtist(id=uuid4(), original_name=name, normalized_name=normalize_artist(name))


def _suggested(result: ArtistMatchResult | None) -> list[tuple[str, float]]:
    assert result is not None
    return [(s.target_id, round(s.score, 2)) for s in result.suggestions]


# --- the floor ------------------------------------------------------------------------------------


def test_the_artist_floor_is_56_as_songs() -> None:
    assert mc.MIN_PRESENTATION_SCORE == 56 == mc.SONG_MIN_PRESENTATION_SCORE
    assert Settings.model_fields["min_presentation_score"].default == 56
    assert ArtistMatchThresholds().min_presentation_score == 56


def test_a_best_fuzzy_score_under_56_is_no_answer() -> None:
    """53.85 was a LOW_CONFIDENCE review under the old floor of 50; now the local tier has no
    answer and the name goes on to the MusicBrainz tier or DEFERRED_RETRY."""
    rodeo = _artist("Blue Rodeo", _mbid())

    assert NormalizationStrategy([rodeo]).apply(_log_name("BLUE OYSTER CULT")) is None


# --- the zones ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("top", "gap", "competitor"),
    [(95.0, 0.0, True), (95.0, 100.0, False), (100.0, 0.0, True), (96.3, 100.0, False)],
)
def test_95_or_more_still_links(top: float, gap: float, competitor: bool) -> None:
    assert _decide_artist_zone(top, gap, 80, 10, competitor) == (
        MatchStatus.AUTO_MATCHED,
        None,
        None,
    )


@pytest.mark.parametrize(
    ("top", "gap"),
    [
        (80.0, 10.0),  # the strong lead rule, at its edges
        (94.99, 14.0),
        (81.82, 43.72),  # MACEO PARKER -> Col. Parker
        (55.0, 5.0),  # the mid-band lead rule, at its edges
        (64.0, 9.0),
        (62.5, 29.17),  # ANTI-FLAG -> Artifas
    ],
)
def test_both_lead_rules_now_suggest(top: float, gap: float) -> None:
    status, reason, detail = _decide_artist_zone(top, gap, 80, 10, True)

    assert (status, reason) == (MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_SUGGESTED)
    assert detail


@pytest.mark.parametrize(
    ("top", "gap", "competitor"),
    [
        (85.0, 3.0, True),  # was AMBIGUOUS_GAP
        (90.0, 100.0, False),  # was LOW_CONFIDENCE: a lone candidate
        (70.0, 2.0, True),  # was LOW_CONFIDENCE
    ],
)
def test_ambiguous_and_low_confidence_results_now_suggest(
    top: float, gap: float, competitor: bool
) -> None:
    status, reason, _ = _decide_artist_zone(top, gap, 80, 10, competitor)

    assert (status, reason) == (MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_SUGGESTED)


# --- the local tier -------------------------------------------------------------------------------


def test_an_exact_name_with_one_artist_still_links() -> None:
    metallica = _artist("Metallica", _mbid())
    local = _artist("Van Halen", None)
    strategy = NormalizationStrategy([metallica, local, _artist("Radiohead", _mbid())])

    by_mbid = strategy.apply(_log_name("METALLICA"))
    by_local_id = strategy.apply(_log_name("VAN HALEN"))

    for result, target in ((by_mbid, metallica.mbid), (by_local_id, local.id)):
        assert result is not None
        assert (result.status, result.confidence_score, result.target_id) == (
            MatchStatus.AUTO_MATCHED,
            100.0,
            target,
        )
        assert not result.suggestions


def test_an_exact_name_with_one_mbid_artist_and_a_local_one_links_the_mbid_artist() -> None:
    """The exact pass still prefers an artist with an MBID to a local-only one of the same name
    (the local one is the library's own copy of the act, not a second act)."""
    nirvana = _artist("Nirvana", _mbid())
    local = _artist("Nirvana", None, normalized=False)

    result = NormalizationStrategy([local, nirvana]).apply(_log_name("NIRVANA"))

    assert result is not None
    assert (result.status, result.target_id) == (MatchStatus.AUTO_MATCHED, nirvana.mbid)


def test_an_exact_name_with_several_artists_is_suggested() -> None:
    """Two acts named Nirvana (the US band and the 1960s UK one): both are suggested at 100,
    in MBID order, and neither is linked."""
    first, second = sorted([_mbid(), _mbid()])
    nirvanas = [_artist("Nirvana", second), _artist("Nirvana", first, normalized=False)]

    result = NormalizationStrategy(nirvanas).apply(_log_name("NIRVANA"))

    assert result is not None
    assert (result.status, result.reason_code) == (
        MatchStatus.NEEDS_REVIEW,
        ReasonCode.LINK_SUGGESTED,
    )
    assert _suggested(result) == [(first, 100.0), (second, 100.0)]
    assert result.target_id == first


def test_an_exact_name_suggests_at_most_three_artists() -> None:
    mbids = sorted(_mbid() for _ in range(4))
    artists = [_artist("Nirvana", mbid, normalized=n == 0) for n, mbid in enumerate(mbids)]

    result = NormalizationStrategy(artists).apply(_log_name("NIRVANA"))

    assert _suggested(result) == [(mbid, 100.0) for mbid in mbids[:3]]


def test_a_fuzzy_score_of_95_or_more_still_links() -> None:
    stones = _artist("The Rolling Stones", _mbid())

    result = NormalizationStrategy([stones, _artist("Radiohead", _mbid())]).apply(
        _log_name("ROLLING STONE")
    )

    assert result is not None
    assert (result.status, round(result.confidence_score, 2), result.target_id) == (
        MatchStatus.AUTO_MATCHED,
        96.3,
        stones.mbid,
    )
    assert not result.suggestions


@pytest.mark.parametrize(("log_name", "stub_name", "score"), E7)
def test_e7_lead_rule_matches_are_suggestions(log_name: str, stub_name: str, score: float) -> None:
    """E7: each of #216's six wrong matches through a lead rule is a suggestion of the named
    stub, the first (here the only one 56 or over)."""
    stub = _artist(stub_name, _mbid())
    pool = [stub, *(_artist(name, _mbid()) for name in FILLERS)]

    result = NormalizationStrategy(pool).apply(_log_name(log_name))

    assert result is not None
    assert (result.status, result.reason_code) == (
        MatchStatus.NEEDS_REVIEW,
        ReasonCode.LINK_SUGGESTED,
    )
    assert _suggested(result) == [(stub.mbid, score)]


def test_fuzzy_suggestions_are_the_three_best_of_56_or_more() -> None:
    """Was AMBIGUOUS_GAP (87.5 leads 81.48 by 6): now the three best, best first; the fourth and
    the one under 56 are left out."""
    pool = [_artist(name, _mbid()) for name, _ in BLUE_OYSTER]
    by_name = {a.name: a.mbid for a in pool}

    result = NormalizationStrategy(pool).apply(_log_name("BLUE OYSTER CULT"))

    assert result is not None
    assert (result.status, result.reason_code) == (
        MatchStatus.NEEDS_REVIEW,
        ReasonCode.LINK_SUGGESTED,
    )
    assert _suggested(result) == [(by_name[name], score) for name, score in BLUE_OYSTER[:3]]
    assert (result.target_id, round(result.confidence_score, 2)) == (
        by_name["Blue Oyster Club"],
        87.5,
    )


# --- the MusicBrainz tier -------------------------------------------------------------------------


def _mb(*hits: tuple[str, int]) -> list[MbArtistResult]:
    return [{"id": mbid, "name": f"Artist {mbid}", "score": score} for mbid, score in hits]


def _mb_answer(*hits: tuple[str, int]) -> ArtistMatchResult | None:
    client = FakeMbClient(responses={TRUNCATED: _mb(*hits)})
    return MusicBrainzApiStrategy(client).apply(_log_name(TRUNCATED))


def test_musicbrainz_95_or_more_still_links() -> None:
    result = _mb_answer(("mb-a", 97))

    assert result is not None
    assert (result.status, result.target_id) == (MatchStatus.AUTO_MATCHED, "mb-a")
    assert not result.suggestions


@pytest.mark.parametrize(
    ("hits", "suggested"),
    [
        ((("mb-a", 85), ("mb-b", 78)), [("mb-a", 85.0), ("mb-b", 78.0)]),  # was AMBIGUOUS_GAP
        ((("mb-a", 62),), [("mb-a", 62.0)]),  # was LOW_CONFIDENCE
        ((("mb-a", 90), ("mb-b", 70)), [("mb-a", 90.0), ("mb-b", 70.0)]),  # the strong lead rule
        (
            (("mb-a", 90), ("mb-b", 80), ("mb-c", 70), ("mb-d", 65)),
            [("mb-a", 90.0), ("mb-b", 80.0), ("mb-c", 70.0)],
        ),
    ],
)
def test_musicbrainz_results_under_95_are_suggestions(
    hits: tuple[tuple[str, int], ...], suggested: list[tuple[str, float]]
) -> None:
    result = _mb_answer(*hits)

    assert result is not None
    assert (result.status, result.reason_code) == (
        MatchStatus.NEEDS_REVIEW,
        ReasonCode.LINK_SUGGESTED,
    )
    assert _suggested(result) == suggested
    # Each suggestion carries its MusicBrainz hit, for the catalog upsert.
    assert [s.mb_candidate["id"] for s in result.suggestions if s.mb_candidate] == [
        mbid for mbid, _ in suggested
    ]


# --- what the run writes --------------------------------------------------------------------------


@dataclass
class _World:
    names: FakeBroadcastArtistRepository
    songs: FakeBroadcastTrackIdentityRepository
    catalog: FakeArtistRepository
    links: FakeLogNameLinkRepository
    playlist_id: UUID
    pending: Callable[[str], tuple[BroadcastArtist, UUID]]
    """A pending name played on the playlist, with one pending song; the name and the song."""

    def add(self, name: str, mbid: str | None = None, *, normalized: bool = True) -> Artist:
        return self.catalog.upsert(_artist(name, mbid or _mbid(), normalized=normalized))

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
            ),
            mb_client=mb_client or FakeMbClient(),
        )

    def name_state(self, name_id: UUID) -> tuple[MatchStatus, ReasonCode | None]:
        name = self.names.get_by_id(name_id)
        assert name is not None
        return name.match_status, name.reason_code

    def song_state(self, song_id: UUID) -> tuple[MatchStatus, ReasonCode | None]:
        song = self.songs.get_by_id(song_id)
        assert song is not None
        return song.match_status, song.reason_code

    def links_of(self, name_id: UUID) -> dict[str, tuple[str, str, int]]:
        return {
            link.artist_id: (link.state.value, link.origin.value, link.strength)
            for link in self.links.list_by_name(name_id)
        }


def _world(names: FakeBroadcastArtistRepository | None = None) -> _World:
    names = names or FakeBroadcastArtistRepository()
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

    return _World(
        names=names,
        songs=songs,
        catalog=catalog,
        links=FakeLogNameLinkRepository(artists=catalog, names=names),
        playlist_id=playlist_id,
        pending=pending,
    )


PENDING_SONG = (MatchStatus.PENDING, None)
SUGGESTED = (MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_SUGGESTED)


def test_e7_names_end_in_review_with_their_stub_suggested() -> None:
    """E7 through a run: no link, the stub suggested, the name in review, no cascade."""
    world = _world()
    for name in FILLERS:
        world.add(name)
    cases = []
    for log_name, stub_name, score in E7:
        stub = world.add(stub_name)
        name, song = world.pending(log_name)
        cases.append((name, song, stub, score))

    world.run()

    for name, song, stub, score in cases:
        assert world.name_state(name.id) == SUGGESTED, name.original_name
        assert world.links_of(name.id) == {stub.id: ("suggested", "automatic", _strength(score))}
        [link] = world.links.list_by_name(name.id)
        assert (link.evidence["version"], link.evidence["tier"]) == (
            0,
            MatchTier.NORMALIZATION.value,
        )
        assert link.evidence["score"] == pytest.approx(score, abs=0.01)
        assert world.song_state(song) == PENDING_SONG


def test_a_run_writes_the_three_best_suggestions() -> None:
    world = _world()
    by_name = {name: world.add(name) for name, _ in BLUE_OYSTER}
    name, song = world.pending("BLUE OYSTER CULT")

    world.run()

    assert world.name_state(name.id) == SUGGESTED
    assert world.links_of(name.id) == {
        by_name[artist].id: ("suggested", "automatic", _strength(score))
        for artist, score in BLUE_OYSTER[:3]
    }
    stored = world.names.get_by_id(name.id)
    assert stored is not None and stored.reason_detail
    assert world.song_state(song) == PENDING_SONG


def test_a_run_suggests_every_artist_an_exact_name_points_at() -> None:
    world = _world()
    first = world.add("Nirvana")
    second = world.add("Nirvana", normalized=False)
    name, song = world.pending("NIRVANA")

    world.run()

    assert world.name_state(name.id) == SUGGESTED
    assert world.links_of(name.id) == {
        first.id: ("suggested", "automatic", 100),
        second.id: ("suggested", "automatic", 100),
    }
    assert world.song_state(song) == PENDING_SONG


def test_a_run_still_links_an_exact_name_and_95_or_more() -> None:
    world = _world()
    metallica = world.add("Metallica")
    stones = world.add("The Rolling Stones")
    exact, _ = world.pending("METALLICA")
    close, _ = world.pending("ROLLING STONE")

    world.run()

    assert world.name_state(exact.id) == (MatchStatus.AUTO_MATCHED, None)
    assert world.links_of(exact.id) == {metallica.id: ("active", "automatic", 100)}
    assert world.name_state(close.id) == (MatchStatus.AUTO_MATCHED, None)
    assert world.links_of(close.id) == {stones.id: ("active", "automatic", 96)}


def test_musicbrainz_suggestions_are_upserted_and_suggested() -> None:
    world = _world()
    name, song = world.pending(TRUNCATED)
    hits = [
        {"id": "mb-a", "name": "Wandering Prairie", "sort-name": "Prairie, Wandering", "score": 85},
        {"id": "mb-b", "name": "Skyline Wanderers", "score": 78},
    ]

    world.run(FakeMbClient(responses={TRUNCATED: hits}))

    assert world.name_state(name.id) == SUGGESTED
    best, second = world.catalog.get_by_mbid("mb-a"), world.catalog.get_by_mbid("mb-b")
    assert best is not None and second is not None
    assert (best.name, best.sort_name) == ("Wandering Prairie", "Prairie, Wandering")
    assert (second.name, second.sort_name) == ("Skyline Wanderers", "Skyline Wanderers")
    assert world.links_of(name.id) == {
        best.id: ("suggested", "automatic", 85),
        second.id: ("suggested", "automatic", 78),
    }
    assert world.song_state(song) == PENDING_SONG


def test_deferred_retry_is_unchanged() -> None:
    """No local answer and not cut off: DEFERRED_RETRY, and its songs deferred, until phase 4."""
    world = _world()
    world.add("Metallica")
    name, song = world.pending("ZZZYX QWERTY")

    world.run()

    assert world.name_state(name.id) == (MatchStatus.NEEDS_REVIEW, ReasonCode.DEFERRED_RETRY)
    assert world.links_of(name.id) == {}
    assert world.song_state(song) == (MatchStatus.NEEDS_REVIEW, ReasonCode.DEFERRED_RETRY)


# --- E3: refusals start no cascade ----------------------------------------------------------------


def _reject(world: _World, name_id: UUID, artist: Artist) -> None:
    """The curator's rejected link to *artist*."""
    seed_link(
        world.links,
        world.catalog,
        name_id,
        artist.id,
        origin=LogNameLinkOrigin.MANUAL,
        state=LogNameLinkState.REJECTED,
    )


def test_refusals_do_not_cascade() -> None:
    """E3: a suggestion of a rejected pair is not written. ANTI-FLAG's only suggestion is the
    pair the curator rejected: LINK_REJECTED. BLUE OYSTER CULT's best is rejected: the next
    ones are suggested, at most three (whether the fourth moves up is the build's choice).
    Neither name's songs change."""
    world = _world()
    for name in FILLERS:
        world.add(name)
    artifas = world.add("Artifas")
    by_name = {name: world.add(name) for name, _ in BLUE_OYSTER}
    only, only_song = world.pending("ANTI-FLAG")
    some, some_song = world.pending("BLUE OYSTER CULT")
    _reject(world, only.id, artifas)
    _reject(world, some.id, by_name["Blue Oyster Club"])

    world.run()

    assert world.name_state(only.id) == (MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_REJECTED)
    assert world.links_of(only.id) == {artifas.id: ("rejected", "manual", 100)}
    assert world.name_state(some.id) == SUGGESTED
    some_links = world.links_of(some.id)
    assert some_links[by_name["Blue Oyster Club"].id] == ("rejected", "manual", 100)
    assert some_links[by_name["Blue Oyster"].id] == ("suggested", "automatic", 81)
    assert some_links[by_name["Bluesy Oysters"].id] == ("suggested", "automatic", 73)
    suggested = [v for v in some_links.values() if v[0] == "suggested"]
    assert len(suggested) <= 3
    assert by_name["Blue Rodeo"].id not in some_links  # under 56
    for song in (only_song, some_song):
        assert world.song_state(song) == PENDING_SONG


def test_a_name_decided_meanwhile_gets_no_suggestion() -> None:
    """AUD-R018: the run's writes are guarded on the pending status it read. A name the curator
    decided while the run matched keeps the decision and gains no link."""

    class DecidedWhileMatching(FakeBroadcastArtistRepository):
        def get_pending_for_playlist(self, playlist_id: UUID) -> list[BroadcastArtist]:
            read = super().get_pending_for_playlist(playlist_id)
            for name in read:
                self.update_match_status(name.id, MatchStatus.MANUAL_REJECTED)
            return read

    world = _world(DecidedWhileMatching())
    for name, _ in BLUE_OYSTER:
        world.add(name)
    name, song = world.pending("BLUE OYSTER CULT")

    world.run()

    assert world.name_state(name.id) == (MatchStatus.MANUAL_REJECTED, None)
    assert world.links_of(name.id) == {}
    assert world.song_state(song) == PENDING_SONG


def test_a_name_decided_meanwhile_gets_no_musicbrainz_upsert() -> None:
    """AUD-R018 for the new MusicBrainz suggestions: a cut-off name the curator decided while
    the run matched gets no suggestion, and its hits are not upserted into the catalog."""

    class DecidedWhileMatching(FakeBroadcastArtistRepository):
        def get_pending_for_playlist(self, playlist_id: UUID) -> list[BroadcastArtist]:
            read = super().get_pending_for_playlist(playlist_id)
            for name in read:
                self.update_match_status(name.id, MatchStatus.MANUAL_MATCHED)
            return read

    world = _world(DecidedWhileMatching())
    name, _ = world.pending(TRUNCATED)

    world.run(FakeMbClient(responses={TRUNCATED: _mb(("mb-a", 85), ("mb-b", 78))}))

    assert world.name_state(name.id) == (MatchStatus.MANUAL_MATCHED, None)
    assert world.links_of(name.id) == {}
    assert world.catalog.musicbrainz_upserts == []
    assert world.catalog.get_by_mbid("mb-a") is None
