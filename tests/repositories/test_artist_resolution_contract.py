"""The artist Resolve and Unmatch port's fake against its PostgreSQL adapter (artist model phase
1a, C75; S9, S10).

Each case seeds log names, songs, song matches and plays through the shared ports, runs the
port's statements and reads the result back. It runs on the in-memory fakes
(``tests/fakes/artist_resolution.py`` over the broadcast and match fakes' rows) and on
PostgreSQL, so the fake cannot drift from the statements the routes ran before 1a. The routes'
own behaviour is pinned by the locked ``tests/routers/test_matching.py`` and
``test_matching_unmatch.py`` and by ``tests/routers/test_resolve_writes_link.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import MatchStatus, MatchTier, ReasonCode
from backend.domain.matching import Match
from backend.repositories.artist_resolution import ArtistResolutionRepository
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.repositories.matches import MatchRepository
from backend.services.repository_factory import RepositoryFactory
from tests.fakes.artist_resolution import FakeArtistResolutionRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.matches import FakeMatchRepository


@dataclass(frozen=True)
class Ports:
    resolution: ArtistResolutionRepository
    names: BroadcastArtistRepository
    songs: BroadcastTrackIdentityRepository
    matches: MatchRepository
    new_playlist: Callable[[], UUID]
    play: Callable[[UUID, UUID], None]
    """Record a play of the song (second) on the playlist (first)."""


def _fake_ports() -> Ports:
    names = FakeBroadcastArtistRepository()
    songs = FakeBroadcastTrackIdentityRepository()
    matches = FakeMatchRepository()
    return Ports(
        resolution=FakeArtistResolutionRepository(names=names, songs=songs, matches=matches),
        names=names,
        songs=songs,
        matches=matches,
        new_playlist=uuid4,
        play=songs.register_playlist_identity,
    )


def _pg_ports(conn: psycopg.Connection[dict[str, object]]) -> Ports:
    factory = RepositoryFactory(conn)

    def new_playlist() -> UUID:
        station = factory.broadcast_stations.create(
            BroadcastStation(id=uuid4(), call_letters=f"K{uuid4().hex[:5]}")
        )
        return factory.broadcast_playlists.create(
            BroadcastPlaylist(
                id=uuid4(), name=f"{uuid4().hex}.csv", content_hash=uuid4().hex,
                station_id=station.id,
            )
        ).id  # fmt: skip

    def play(playlist_id: UUID, song_id: UUID) -> None:
        factory.broadcast_events.create(
            BroadcastPlayEvent(
                id=uuid4(),
                identity_id=song_id,
                playlist_id=playlist_id,
                played_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        )

    return Ports(
        resolution=factory.broadcast.resolution,
        names=factory.broadcast_artists,
        songs=factory.broadcast_identities,
        matches=factory.matches,
        new_playlist=new_playlist,
        play=play,
    )


@pytest.fixture(params=["fake", pytest.param("pg", marks=pytest.mark.integration)])
def ports(request: pytest.FixtureRequest) -> Iterator[Ports]:
    if request.param == "fake":
        yield _fake_ports()
        return
    migrated_db: str = request.getfixturevalue("migrated_db")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield _pg_ports(conn)
        conn.rollback()


# --- seeds and reads ---------------------------------------------------------------------------


def _name(
    ports: Ports, status: MatchStatus, reason: ReasonCode | None = ReasonCode.LOW_CONFIDENCE
) -> UUID:
    spelling = f"NAME {uuid4().hex[:8]}"
    name = ports.names.upsert(
        BroadcastArtist(id=uuid4(), original_name=spelling, normalized_name=spelling.lower())
    )
    ports.names.update_match_status(name.id, status, reason, None if reason is None else "why")
    return name.id


def _song(ports: Ports, name_id: UUID, status: MatchStatus) -> UUID:
    song = ports.songs.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=name_id,
            original_title="Song",
            normalized_title="song",
            normalized_signature=uuid4().hex,
        )
    )
    if status != MatchStatus.PENDING:
        ports.songs.update_match_status(
            song.id, status, MatchTier.LOCAL_FILE_FUZZY, ReasonCode.LOW_CONFIDENCE, "stale"
        )
    return song.id


def _suggest(ports: Ports, song_id: UUID) -> None:
    ports.matches.create(
        Match(
            id=uuid4(),
            identity_id=song_id,
            confidence_score=60.0,
            match_tier=MatchTier.LOCAL_FILE_FUZZY,
        )
    )


def _state(ports: Ports, song_id: UUID) -> tuple[object, ...]:
    song = ports.songs.get_by_id(song_id)
    assert song is not None
    has_match = ports.matches.get_by_identity(song_id) is not None
    return song.match_status, song.match_tier, song.reason_code, song.reason_detail, has_match


def _name_state(ports: Ports, name_id: UUID) -> tuple[object, ...]:
    name = ports.names.get_by_id(name_id)
    assert name is not None
    return name.match_status, name.reason_code, name.reason_detail


STALE = (MatchTier.LOCAL_FILE_FUZZY, ReasonCode.LOW_CONFIDENCE, "stale")


# --- the cases ---------------------------------------------------------------------------------


def test_exists_and_status_of(ports: Ports) -> None:
    name_id = _name(ports, MatchStatus.AUTO_MATCHED)

    assert (ports.resolution.exists(name_id), ports.resolution.exists(uuid4())) == (True, False)
    assert ports.resolution.status_of(name_id) == MatchStatus.AUTO_MATCHED
    assert ports.resolution.status_of(uuid4()) is None


def test_set_status_writes_the_status_only(ports: Ports) -> None:
    name_id = _name(ports, MatchStatus.NEEDS_REVIEW)

    ports.resolution.set_status(name_id, MatchStatus.MANUAL_MATCHED)

    assert _name_state(ports, name_id) == (
        MatchStatus.MANUAL_MATCHED,
        ReasonCode.LOW_CONFIDENCE,
        "why",
    )


def test_a_manual_match_reopens_the_songs_in_review_and_drops_their_matches(ports: Ports) -> None:
    name_id, other = _name(ports, MatchStatus.AUTO_MATCHED), _name(ports, MatchStatus.AUTO_MATCHED)
    review = _song(ports, name_id, MatchStatus.NEEDS_REVIEW)
    pending = _song(ports, name_id, MatchStatus.PENDING)
    matched = _song(ports, name_id, MatchStatus.AUTO_MATCHED)
    elsewhere = _song(ports, other, MatchStatus.PENDING)
    for song_id in (review, pending, matched, elsewhere):
        _suggest(ports, song_id)

    ports.resolution.reopen_songs_in_review(name_id)
    ports.resolution.delete_pending_song_matches(name_id)

    assert _state(ports, review) == (MatchStatus.PENDING, None, None, None, False)
    assert _state(ports, pending) == (MatchStatus.PENDING, None, None, None, False)
    assert _state(ports, matched) == (MatchStatus.AUTO_MATCHED, *STALE, True)
    assert _state(ports, elsewhere) == (MatchStatus.PENDING, None, None, None, True)


def test_the_playlists_playing_the_name(ports: Ports) -> None:
    name_id, other = _name(ports, MatchStatus.AUTO_MATCHED), _name(ports, MatchStatus.PENDING)
    first, second, third = (ports.new_playlist() for _ in range(3))
    one, two = (
        _song(ports, name_id, MatchStatus.PENDING),
        _song(ports, name_id, MatchStatus.PENDING),
    )
    ports.play(first, one)
    ports.play(first, two)
    ports.play(second, two)
    ports.play(third, _song(ports, other, MatchStatus.PENDING))

    assert sorted(ports.resolution.playlists_playing(name_id)) == sorted([first, second])
    assert ports.resolution.playlists_playing(uuid4()) == []


def test_a_manual_reject_rejects_every_song_but_the_curators(ports: Ports) -> None:
    name_id = _name(ports, MatchStatus.AUTO_MATCHED)
    songs = {status: _song(ports, name_id, status) for status in MatchStatus}

    ports.resolution.reject_open_songs(name_id)

    kept = {MatchStatus.MANUAL_MATCHED, MatchStatus.MANUAL_REJECTED}
    for status, song_id in songs.items():
        expected = status if status in kept else MatchStatus.AUTO_REJECTED
        song = ports.songs.get_by_id(song_id)
        assert song is not None and song.match_status == expected, status


@pytest.mark.parametrize("status", list(MatchStatus))
def test_unmatch_takes_only_a_decided_name(ports: Ports, status: MatchStatus) -> None:
    name_id = _name(ports, status)
    decided = status not in {MatchStatus.PENDING, MatchStatus.NEEDS_REVIEW}

    assert ports.resolution.unmatch_if_decided(name_id) is decided

    if decided:
        assert _name_state(ports, name_id) == (
            MatchStatus.NEEDS_REVIEW,
            ReasonCode.USER_UNMATCHED,
            None,
        )
    else:
        assert _name_state(ports, name_id) == (status, ReasonCode.LOW_CONFIDENCE, "why")
    assert ports.resolution.unmatch_if_decided(uuid4()) is False


def test_unmatch_reopens_every_song_and_drops_every_match(ports: Ports) -> None:
    name_id, other = _name(ports, MatchStatus.AUTO_MATCHED), _name(ports, MatchStatus.AUTO_MATCHED)
    songs = [_song(ports, name_id, status) for status in MatchStatus]
    elsewhere = _song(ports, other, MatchStatus.AUTO_MATCHED)
    for song_id in (*songs, elsewhere):
        _suggest(ports, song_id)

    ports.resolution.reopen_every_song(name_id)
    ports.resolution.delete_song_matches(name_id)

    reopened = (MatchStatus.NEEDS_REVIEW, None, ReasonCode.USER_UNMATCHED, None, False)
    assert [_state(ports, song_id) for song_id in songs] == [reopened] * len(songs)
    assert _state(ports, elsewhere) == (MatchStatus.AUTO_MATCHED, *STALE, True)
