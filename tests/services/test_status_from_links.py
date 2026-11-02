"""A log name's status comes from its links (artist model phase 1c, SPEC S3; C36, AUD-R038).

``LogNameLinkService`` sets the name's status in the same transaction as any link change, by one
rule, ``status_from_links`` (``backend/domain/log_name_status.py``), applied in this order:

1. a user action writes the status it names: Resolve ``manual_matched``, Reject
   ``manual_rejected``, Unmatch ``needs_review`` / ``USER_UNMATCHED`` (their machine reason is
   cleared);
2. a ``manual_rejected`` or ``auto_rejected`` name keeps its status, whatever its links;
3. any active manual link: ``manual_matched``;
4. any active link: ``auto_matched``;
5. any suggestion: ``needs_review`` / ``LINK_SUGGESTED``;
6. the matcher's only answer was a rejected pair: ``needs_review`` / ``LINK_REJECTED`` (1b);
7. a matched name left with no link: ``needs_review`` / ``LINK_REMOVED``;
8. anything else (pending, ``needs_review`` with another reason, ``DEFERRED_RETRY``) stays as its
   last writer set it; the matcher is the writer of its own answer (``DEFERRED_RETRY``).

Matcher writes keep the AUD-R018 guard, Unmatch keeps its decided-only write (a second Unmatch
is refused, 1b's E3), and the re-check's rewind to pending stays a writer. Status writes leave
``_persist_artist_result`` and ``artist_resolution_service``: both reach the name's status only
through the link service. A listed placeholder (PR 2) is not here yet.

The PostgreSQL half, over every row, is ``tests/integration/test_status_from_links_pg.py``.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.catalog import Artist
from backend.domain.enums import (
    CatalogSource,
    LogNameLinkOrigin,
    LogNameLinkState,
    MatchStatus,
    ReasonCode,
)
from backend.domain.log_name_status import NameState, status_from_links
from backend.domain.matching import LogNameLink, LogNameNotUnmatchableError
from backend.services import artist_matching_service, artist_resolution_service
from backend.services.artist_matching_service import ArtistMatchingRepos, match_artists_for_playlist
from backend.services.artist_resolution_service import (
    ArtistResolutionRepos,
    match_by_hand,
    reject_by_hand,
    unmatch,
)
from backend.services.normalization import compute_normalized_signature, normalize_artist
from tests.fakes.artist_cascade import FakeArtistCascadeRepository
from tests.fakes.artist_resolution import FakeArtistResolutionRepository
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_play_events import FakeBroadcastPlayEventRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.log_name_links import FakeLogNameLinkRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient
from tests.matching_seed import seed_link

NAME = uuid4()
ACTIVE, SUGGESTED, REJECTED = (
    LogNameLinkState.ACTIVE,
    LogNameLinkState.SUGGESTED,
    LogNameLinkState.REJECTED,
)
MANUAL, AUTOMATIC, LEGACY = (
    LogNameLinkOrigin.MANUAL,
    LogNameLinkOrigin.AUTOMATIC,
    LogNameLinkOrigin.LEGACY,
)
REJECTED_STATUSES = (MatchStatus.MANUAL_REJECTED, MatchStatus.AUTO_REJECTED)
MATCHED_STATUSES = (MatchStatus.AUTO_MATCHED, MatchStatus.MANUAL_MATCHED)

PENDING = NameState(MatchStatus.PENDING)
DEFERRED = NameState(MatchStatus.NEEDS_REVIEW, ReasonCode.DEFERRED_RETRY, "deferred")
HELD_BACK = NameState(MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_REJECTED, "held back")
LOW = NameState(MatchStatus.NEEDS_REVIEW, ReasonCode.LOW_CONFIDENCE, "Score 62%")
UNMATCHED = NameState(MatchStatus.NEEDS_REVIEW, ReasonCode.USER_UNMATCHED)


def _link(state: LogNameLinkState, origin: LogNameLinkOrigin, strength: int = 80) -> LogNameLink:
    return LogNameLink(
        broadcast_artist_id=NAME,
        artist_id=str(uuid4()),
        state=state,
        origin=origin,
        strength=strength,
        evidence={"version": 0},
    )


def _code(state: NameState) -> tuple[MatchStatus, ReasonCode | None]:
    return state.status, state.reason_code


# --- the rule -------------------------------------------------------------------------------------

EVERY_START = [
    PENDING,
    DEFERRED,
    LOW,
    UNMATCHED,
    NameState(MatchStatus.AUTO_MATCHED),
    NameState(MatchStatus.MANUAL_MATCHED),
    NameState(MatchStatus.AUTO_REJECTED),
    NameState(MatchStatus.MANUAL_REJECTED),
]
EVERY_LINK_SET = [
    [],
    [_link(ACTIVE, MANUAL)],
    [_link(ACTIVE, AUTOMATIC), _link(SUGGESTED, LEGACY)],
    [_link(SUGGESTED, AUTOMATIC)],
    [_link(REJECTED, MANUAL)],
]


@pytest.mark.parametrize("current", EVERY_START)
@pytest.mark.parametrize("links", EVERY_LINK_SET)
@pytest.mark.parametrize(
    "action",
    [
        NameState(MatchStatus.MANUAL_MATCHED),  # Resolve
        NameState(MatchStatus.MANUAL_REJECTED),  # Reject
        UNMATCHED,  # Unmatch
    ],
)
def test_1_a_user_action_writes_the_status_it_names(
    current: NameState, links: list[LogNameLink], action: NameState
) -> None:
    assert status_from_links(current, links, action, by_user=True) == action


@pytest.mark.parametrize("status", REJECTED_STATUSES)
@pytest.mark.parametrize("links", EVERY_LINK_SET)
@pytest.mark.parametrize("proposed", [None, DEFERRED, HELD_BACK])
def test_2_a_rejected_name_keeps_its_status_whatever_its_links(
    status: MatchStatus, links: list[LogNameLink], proposed: NameState | None
) -> None:
    current = NameState(status, None, "kept")

    assert status_from_links(current, links, proposed) == current


NOT_REJECTED = [s for s in EVERY_START if s.status not in REJECTED_STATUSES]


@pytest.mark.parametrize("current", NOT_REJECTED)
@pytest.mark.parametrize("proposed", [None, DEFERRED, HELD_BACK])
@pytest.mark.parametrize(
    ("links", "expected"),
    [
        # 3: an active manual link wins over everything below it.
        ([_link(ACTIVE, MANUAL)], MatchStatus.MANUAL_MATCHED),
        ([_link(ACTIVE, AUTOMATIC), _link(ACTIVE, MANUAL)], MatchStatus.MANUAL_MATCHED),
        ([_link(ACTIVE, MANUAL), _link(SUGGESTED, LEGACY)], MatchStatus.MANUAL_MATCHED),
        # 4: any other active link, whatever its origin.
        ([_link(ACTIVE, AUTOMATIC)], MatchStatus.AUTO_MATCHED),
        ([_link(ACTIVE, LEGACY), _link(SUGGESTED, AUTOMATIC)], MatchStatus.AUTO_MATCHED),
        ([_link(ACTIVE, LogNameLinkOrigin.ONE_TIME_PASS)], MatchStatus.AUTO_MATCHED),
        ([_link(ACTIVE, AUTOMATIC), _link(REJECTED, MANUAL)], MatchStatus.AUTO_MATCHED),
    ],
)
def test_3_4_an_active_link_matches_the_name(
    current: NameState,
    proposed: NameState | None,
    links: list[LogNameLink],
    expected: MatchStatus,
) -> None:
    assert _code(status_from_links(current, links, proposed)) == (expected, None)


@pytest.mark.parametrize("current", NOT_REJECTED)
@pytest.mark.parametrize("proposed", [None, DEFERRED, HELD_BACK])
def test_5_suggestions_put_the_name_in_review(
    current: NameState, proposed: NameState | None
) -> None:
    """A deferred or held-back answer of the matcher does not hide the name's suggestions, and
    starts no cascade (the matcher cascades only DEFERRED_RETRY it wrote)."""
    links = [
        _link(SUGGESTED, AUTOMATIC, 62),
        _link(SUGGESTED, LEGACY, 85),
        _link(REJECTED, MANUAL, 100),
    ]

    state = status_from_links(current, links, proposed)

    assert _code(state) == (MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_SUGGESTED)
    assert state.reason_detail  # says what is suggested, said when written


@pytest.mark.parametrize("links", [[], [_link(REJECTED, MANUAL)]])
def test_6_a_held_back_answer_is_link_rejected(links: list[LogNameLink]) -> None:
    assert status_from_links(PENDING, links, HELD_BACK) == HELD_BACK


@pytest.mark.parametrize("status", MATCHED_STATUSES)
@pytest.mark.parametrize("links", [[], [_link(REJECTED, MANUAL)], [_link(REJECTED, LEGACY)]])
def test_7_a_matched_name_left_with_no_link_is_link_removed(
    status: MatchStatus, links: list[LogNameLink]
) -> None:
    state = status_from_links(NameState(status), links)

    assert _code(state) == (MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_REMOVED)
    assert state.reason_detail


@pytest.mark.parametrize("current", [PENDING, DEFERRED, LOW, UNMATCHED, HELD_BACK])
@pytest.mark.parametrize("links", [[], [_link(REJECTED, MANUAL)]])
def test_8_otherwise_the_last_writer_stands(current: NameState, links: list[LogNameLink]) -> None:
    assert status_from_links(current, links) == current


@pytest.mark.parametrize("links", [[], [_link(REJECTED, MANUAL)]])
def test_8_the_matcher_writes_its_own_answer(links: list[LogNameLink]) -> None:
    assert status_from_links(PENDING, links, DEFERRED) == DEFERRED


def test_the_suggestion_detail_counts_the_suggestions_and_names_the_best_score() -> None:
    links = [_link(SUGGESTED, AUTOMATIC, 62), _link(SUGGESTED, LEGACY, 85)]

    detail = status_from_links(PENDING, links).reason_detail

    assert detail == "2 suggested artists, best 85%"
    assert status_from_links(PENDING, links[:1]).reason_detail == "1 suggested artist, best 62%"


# --- the writers ----------------------------------------------------------------------------------


@dataclass
class _World:
    names: FakeBroadcastArtistRepository
    songs: FakeBroadcastTrackIdentityRepository
    matches: FakeMatchRepository
    catalog: FakeArtistRepository
    links: FakeLogNameLinkRepository
    repos: ArtistResolutionRepos
    playlist_id: UUID

    def name(self, spelling: str, status: MatchStatus = MatchStatus.PENDING) -> UUID:
        stored = self.names.upsert(
            BroadcastArtist(
                id=uuid4(), original_name=spelling, normalized_name=normalize_artist(spelling)
            )
        )
        self.names.update_match_status(stored.id, status, ReasonCode.LOW_CONFIDENCE, "stale")
        self.names.register_playlist_artist(self.playlist_id, stored.id)
        return stored.id

    def song(self, name_id: UUID) -> UUID:
        name = self.names.get_by_id(name_id)
        assert name is not None
        song = self.songs.upsert(
            BroadcastTrackIdentity(
                id=uuid4(),
                broadcast_artist_id=name_id,
                original_title="Song",
                normalized_title="song",
                normalized_signature=compute_normalized_signature(name.normalized_name, "song"),
            )
        )
        self.songs.register_playlist_identity(self.playlist_id, song.id)
        return song.id

    def artist(self, name: str) -> Artist:
        return self.catalog.upsert(
            Artist(
                id=str(uuid4()),
                name=name,
                sort_name=name,
                normalized_name=normalize_artist(name),
                mbid=str(uuid4()),
                origin=CatalogSource.MUSICBRAINZ,
                needs_enhancement=False,
            )
        )

    def link(
        self,
        name_id: UUID,
        artist: Artist,
        state: LogNameLinkState,
        origin: LogNameLinkOrigin = LEGACY,
        strength: int = 60,
    ) -> None:
        seed_link(
            self.links,
            self.catalog,
            name_id,
            artist.id,
            origin=origin,
            state=state,
            strength=strength,
        )

    def state(self, name_id: UUID) -> tuple[MatchStatus, ReasonCode | None, str | None]:
        name = self.names.get_by_id(name_id)
        assert name is not None
        return name.match_status, name.reason_code, name.reason_detail

    def song_status(self, song_id: UUID) -> MatchStatus:
        song = self.songs.get_by_id(song_id)
        assert song is not None
        return song.match_status

    def link_states(self, name_id: UUID) -> dict[str, tuple[str, str]]:
        return {
            link.artist_id: (link.state.value, link.origin.value)
            for link in self.links.list_by_name(name_id)
        }

    def run_matcher(self) -> None:
        match_artists_for_playlist(
            playlist_id=self.playlist_id,
            repos=ArtistMatchingRepos(
                broadcast_artist_repo=self.names,
                track_identity_repo=self.songs,
                artist_repo=self.catalog,
                match_repo=self.matches,
                rules_repo=FakeMappingRuleRepository(),
                link_repo=self.links,
            ),
            mb_client=FakeMbClient(),
        )


@pytest.fixture
def world() -> _World:
    names = FakeBroadcastArtistRepository()
    songs = FakeBroadcastTrackIdentityRepository()
    matches = FakeMatchRepository()
    catalog = FakeArtistRepository()
    links = FakeLogNameLinkRepository(artists=catalog, names=names)
    repos = ArtistResolutionRepos(
        resolution=FakeArtistResolutionRepository(names=names, songs=songs, matches=matches),
        links=links,
        catalog=catalog,
        cascade=FakeArtistCascadeRepository(
            names=names,
            songs=songs,
            matches=matches,
            files=FakeLibraryFileRepository(),
            plays=FakeBroadcastPlayEventRepository(),
        ),
        commit=lambda: None,
        names=names,
    )
    return _World(names, songs, matches, catalog, links, repos, uuid4())


EVERY_STATUS = list(MatchStatus)


@pytest.mark.parametrize("status", EVERY_STATUS)
def test_resolve_writes_manual_matched(world: _World, status: MatchStatus) -> None:
    """Accepting a suggestion is the manual resolve: the link becomes active and manual."""
    beth_hart = world.artist("Beth Hart")
    name_id = world.name("BETH HART", status)
    world.link(name_id, beth_hart, SUGGESTED)

    match_by_hand(name_id, beth_hart.id, world.repos)

    assert world.state(name_id) == (MatchStatus.MANUAL_MATCHED, None, None)
    assert world.link_states(name_id) == {beth_hart.id: ("active", "manual")}


@pytest.mark.parametrize("status", EVERY_STATUS)
def test_reject_writes_manual_rejected(world: _World, status: MatchStatus) -> None:
    artist = world.artist("Beth Hart")
    name_id = world.name("BETH HART", status)
    world.link(name_id, artist, ACTIVE)

    reject_by_hand(name_id, world.repos)

    assert world.state(name_id) == (MatchStatus.MANUAL_REJECTED, None, None)
    assert world.link_states(name_id) == {artist.id: ("rejected", "legacy")}


@pytest.mark.parametrize("status", [*MATCHED_STATUSES, *REJECTED_STATUSES])
def test_unmatch_writes_user_unmatched_once(world: _World, status: MatchStatus) -> None:
    artist = world.artist("Beth Hart")
    name_id = world.name("BETH HART", status)
    world.link(name_id, artist, ACTIVE, MANUAL)
    world.link(name_id, world.artist("Beth Harte"), SUGGESTED)

    unmatch(name_id, world.repos)

    assert world.state(name_id) == (MatchStatus.NEEDS_REVIEW, ReasonCode.USER_UNMATCHED, None)
    assert {state for state, _ in world.link_states(name_id).values()} == {"rejected"}
    with pytest.raises(LogNameNotUnmatchableError):
        unmatch(name_id, world.repos)  # 1b's E3: the decided-only write refuses a second


def test_the_matcher_keeps_a_rewound_names_suggestion_in_review(world: _World) -> None:
    """A suggested name the re-check rewound to pending, which the matcher now defers (its
    suggested artist, B.O.C., scores 21 against the name, so the matcher finds nothing):
    LINK_SUGGESTED, not DEFERRED_RETRY, and no cascade: the song stays pending."""
    gone = world.artist("B.O.C.")
    name_id = world.name("BLUE OYSTER CULT")
    song_id = world.song(name_id)
    world.link(name_id, gone, SUGGESTED, strength=58)

    world.run_matcher()

    status, reason, detail = world.state(name_id)
    assert (status, reason) == (MatchStatus.NEEDS_REVIEW, ReasonCode.LINK_SUGGESTED)
    assert detail == "1 suggested artist, best 58%"
    assert world.song_status(song_id) == MatchStatus.PENDING


def test_the_matcher_cannot_unseat_a_manual_link(world: _World) -> None:
    """Rule 3 through the matcher: a name holding an active manual link is manual_matched,
    whatever the matcher adds (here an exact automatic link to another artist)."""
    manual = world.artist("Mellencamp")
    exact = world.artist("John Mellencamp")
    name_id = world.name("JOHN MELLENCAMP")
    world.link(name_id, manual, ACTIVE, MANUAL, strength=100)

    world.run_matcher()

    assert world.state(name_id)[:2] == (MatchStatus.MANUAL_MATCHED, None)
    assert world.link_states(name_id) == {
        manual.id: ("active", "manual"),
        exact.id: ("active", "automatic"),
    }


def test_the_matcher_writes_deferred_retry_itself(world: _World) -> None:
    name_id = world.name("ZZZYX QWERTY")
    song_id = world.song(name_id)

    world.run_matcher()

    assert world.state(name_id)[:2] == (MatchStatus.NEEDS_REVIEW, ReasonCode.DEFERRED_RETRY)
    assert world.song_status(song_id) == MatchStatus.NEEDS_REVIEW  # deferred, as before


# --- where the status is written ------------------------------------------------------------------

STATUS_WRITERS = {"update_match_status", "update_match_status_if_pending", "set_status"}


def _status_writes(function: object) -> list[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))  # type: ignore[arg-type]
    return [
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in STATUS_WRITERS
    ]


def test_status_writes_leave_the_matcher_and_the_resolution_service() -> None:
    """Both reach the name's status only through the link service. Unmatch's decided-only
    write (``unmatch_if_decided``) stays, as the 409's guard."""
    assert _status_writes(artist_matching_service._persist_artist_result) == []
    assert _status_writes(artist_resolution_service) == []


def test_the_scan_sees_a_status_write() -> None:
    def writes(repos: ArtistResolutionRepos, name_id: UUID) -> None:
        repos.resolution.set_status(name_id, MatchStatus.MANUAL_MATCHED)

    assert _status_writes(writes) == ["repos.resolution.set_status"]
