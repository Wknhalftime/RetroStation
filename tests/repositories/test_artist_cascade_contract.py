"""The whole-name cascade port's fake against its PostgreSQL adapter (artist model phase 1b, S1,
S2, S3, S5).

``ArtistCascadeRepository`` reads a log name's lines for ``plan_cascade`` (S1) and writes what
the plan decides; the plan itself is the domain's. Each case seeds log names, lines, files,
matches and plays through the shared ports, runs the port and reads the result back, on the
in-memory fakes (``tests/fakes/artist_cascade.py``) and on PostgreSQL.

- ``lines_of``: every line of the name and no other, each with its status, its plays (every
  play event of the line) and whether its match is to a same-name file: the match
  ``play_file_resolution`` plays (a match to a file, the highest score first, then the oldest)
  is to a file whose ``normalized_artist_name`` is the name's ``normalized_name``.
- ``reject_lines``: the lines become ``auto_rejected``, and nothing else of them changes (as
  today's Reject cascade).
- ``reopen_lines``: the lines become ``needs_review`` with ``USER_UNMATCHED``, tier and detail
  cleared (as today's Unmatch cascade).
- ``delete_matches_of_lines``: every match row of those lines goes.

No write touches a line it was not given. On PostgreSQL, ``lines_of(..., lock=True)`` holds the
name's lines until the transaction ends, so a matcher's write waits for the action (AUD-R018).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.artist_cascade import CascadeLine
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, ReasonCode, TargetType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import Match
from backend.repositories.artist_cascade import ArtistCascadeRepository
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.matches import MatchRepository
from backend.services.normalization import normalize_artist
from backend.services.repository_factory import RepositoryFactory
from tests.fakes.artist_cascade import FakeArtistCascadeRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_play_events import FakeBroadcastPlayEventRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository

_EPOCH = datetime(2026, 10, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Ports:
    cascade: ArtistCascadeRepository
    names: BroadcastArtistRepository
    songs: BroadcastTrackIdentityRepository
    matches: MatchRepository
    files: LibraryFileRepository
    play: Callable[[UUID, int], None]
    """Record that many plays of the song."""


def _fake_ports() -> Ports:
    names = FakeBroadcastArtistRepository()
    songs = FakeBroadcastTrackIdentityRepository()
    matches = FakeMatchRepository()
    files = FakeLibraryFileRepository()
    plays = FakeBroadcastPlayEventRepository()
    playlist = uuid4()

    def play(song_id: UUID, times: int) -> None:
        for n in range(times):
            plays.create(
                BroadcastPlayEvent(
                    id=uuid4(),
                    identity_id=song_id,
                    playlist_id=playlist,
                    played_at=_EPOCH + timedelta(minutes=n),
                )
            )

    return Ports(
        cascade=FakeArtistCascadeRepository(
            names=names, songs=songs, matches=matches, files=files, plays=plays
        ),
        names=names,
        songs=songs,
        matches=matches,
        files=files,
        play=play,
    )


def _pg_ports(conn: psycopg.Connection[Any]) -> Ports:
    factory = RepositoryFactory(conn)
    station = factory.broadcast_stations.create(BroadcastStation(id=uuid4(), call_letters="KCNT"))
    playlists = [
        factory.broadcast_playlists.create(
            BroadcastPlaylist(
                id=uuid4(), name=f"{n}.csv", content_hash=uuid4().hex, station_id=station.id
            )
        ).id
        for n in range(2)
    ]

    def play(song_id: UUID, times: int) -> None:
        for n in range(times):
            factory.broadcast_events.create(
                BroadcastPlayEvent(
                    id=uuid4(),
                    identity_id=song_id,
                    playlist_id=playlists[n % 2],  # plays on two playlists all count
                    played_at=_EPOCH + timedelta(minutes=n),
                )
            )

    return Ports(
        cascade=factory.broadcast.cascade,
        names=factory.broadcast_artists,
        songs=factory.broadcast_identities,
        matches=factory.matches,
        files=factory.library_files,
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


# --- seeds and reads -----------------------------------------------------------------------------


def _name(ports: Ports, spelling: str = "Cascade Band") -> UUID:
    return ports.names.upsert(
        BroadcastArtist(
            id=uuid4(), original_name=spelling, normalized_name=normalize_artist(spelling)
        )
    ).id


def _line(
    ports: Ports,
    name_id: UUID,
    status: MatchStatus,
    *,
    plays: int = 0,
    tier: MatchTier | None = MatchTier.LOCAL_FILE_FUZZY,
    reason: ReasonCode | None = ReasonCode.LOW_CONFIDENCE,
) -> UUID:
    song = ports.songs.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=name_id,
            original_title="Song",
            normalized_title="song",
            normalized_signature=uuid4().hex,
        )
    )
    if status != MatchStatus.PENDING or tier is not None:
        ports.songs.update_match_status(song.id, status, tier, reason, "why")
    ports.play(song.id, plays)
    return song.id


def _file(ports: Ports, artist: str | None) -> UUID:
    return ports.files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=f"/music/{uuid4().hex}.flac",
            format="flac",
            enrichment_status=EnrichmentStatus.PENDING,
            audio=AudioMetadata(
                track_title="Song",
                artist_name=artist,
                normalized_artist_name=None if artist is None else normalize_artist(artist),
            ),
        )
    ).id


def _match(
    ports: Ports,
    line_id: UUID,
    file_id: UUID | None,
    score: float = 0.9,
    created_at: datetime = _EPOCH,
) -> None:
    ports.matches.create(
        Match(
            id=uuid4(),
            confidence_score=score,
            match_tier=MatchTier.LOCAL_FILE_FUZZY,
            identity_id=line_id,
            library_file_id=file_id,
            target_type=TargetType.LIBRARY_FILE,
            created_at=created_at,
        )
    )


def _lines(ports: Ports, name_id: UUID, *, lock: bool = False) -> dict[UUID, CascadeLine]:
    return {line.identity_id: line for line in ports.cascade.lines_of(name_id, lock=lock)}


def _song(ports: Ports, line_id: UUID) -> tuple[Any, ...]:
    song = ports.songs.get_by_id(line_id)
    assert song is not None
    return song.match_status, song.match_tier, song.reason_code, song.reason_detail


# --- lines_of ------------------------------------------------------------------------------------


def test_lines_of_reads_every_line_of_the_name(ports: Ports) -> None:
    name = _name(ports)
    other = _name(ports, "Other Band")
    pending = _line(ports, name, MatchStatus.PENDING, tier=None, reason=None)
    matched = _line(ports, name, MatchStatus.AUTO_MATCHED, plays=3)
    by_hand = _line(ports, name, MatchStatus.MANUAL_MATCHED, plays=1, tier=MatchTier.MANUAL)
    _line(ports, other, MatchStatus.AUTO_MATCHED, plays=5)

    lines = _lines(ports, name)

    assert set(lines) == {pending, matched, by_hand}
    assert {i: (line.status, line.plays) for i, line in lines.items()} == {
        pending: (MatchStatus.PENDING, 0),
        matched: (MatchStatus.AUTO_MATCHED, 3),
        by_hand: (MatchStatus.MANUAL_MATCHED, 1),
    }


def test_a_name_with_no_lines_and_an_unknown_name_read_empty(ports: Ports) -> None:
    assert ports.cascade.lines_of(_name(ports)) == []
    assert ports.cascade.lines_of(uuid4()) == []


def test_same_name_is_the_playing_match_to_a_same_name_file(ports: Ports) -> None:
    name = _name(ports, "CASCADE BAND")
    lines = {
        key: _line(ports, name, MatchStatus.AUTO_MATCHED)
        for key in (
            "same",
            "other",
            "untagged",
            "no_match",
            "match_without_file",
            "best_is_same",
            "best_is_other",
            "tie_oldest_is_same",
        )
    }
    _match(ports, lines["same"], _file(ports, "Cascade Band"))
    _match(ports, lines["other"], _file(ports, "Someone Else"))
    _match(ports, lines["untagged"], _file(ports, None))
    _match(ports, lines["match_without_file"], None)
    _match(ports, lines["best_is_same"], _file(ports, "Cascade Band"), score=0.95)
    _match(ports, lines["best_is_same"], _file(ports, "Someone Else"), score=0.80)
    _match(ports, lines["best_is_other"], _file(ports, "Cascade Band"), score=0.80)
    _match(ports, lines["best_is_other"], _file(ports, "Someone Else"), score=0.95)
    later = _EPOCH + timedelta(hours=1)
    _match(ports, lines["tie_oldest_is_same"], _file(ports, "Someone Else"), created_at=later)
    _match(ports, lines["tie_oldest_is_same"], _file(ports, "Cascade Band"), created_at=_EPOCH)

    read = _lines(ports, name)

    assert {key: read[line].same_name for key, line in lines.items()} == {
        "same": True,
        "other": False,
        "untagged": False,
        "no_match": False,
        "match_without_file": False,
        "best_is_same": True,
        "best_is_other": False,
        "tie_oldest_is_same": True,
    }


def test_same_name_is_read_whatever_the_status(ports: Ports) -> None:
    """The port reports the file's name for every line; ``plan_cascade`` decides what a line in
    review with a same-name candidate means (S1)."""
    name = _name(ports)
    in_review = _line(ports, name, MatchStatus.NEEDS_REVIEW)
    _match(ports, in_review, _file(ports, "Cascade Band"))

    assert _lines(ports, name)[in_review].same_name is True


# --- writes --------------------------------------------------------------------------------------


def test_reject_lines_changes_only_their_status(ports: Ports) -> None:
    name = _name(ports)
    given = _line(ports, name, MatchStatus.AUTO_MATCHED)
    also = _line(ports, name, MatchStatus.NEEDS_REVIEW, tier=None)
    left = _line(ports, name, MatchStatus.AUTO_MATCHED)
    _match(ports, given, _file(ports, "Elsewhere"))
    before = {line: _song(ports, line) for line in (given, also, left)}

    ports.cascade.reject_lines([given, also])

    for line in (given, also):
        assert _song(ports, line) == (MatchStatus.AUTO_REJECTED, *before[line][1:])
    assert _song(ports, left) == before[left]
    assert ports.matches.get_by_identity(given) is not None


def test_reopen_lines_puts_them_in_review_as_user_unmatched(ports: Ports) -> None:
    name = _name(ports)
    given = _line(ports, name, MatchStatus.AUTO_MATCHED)
    left = _line(ports, name, MatchStatus.AUTO_MATCHED)
    before = _song(ports, left)

    ports.cascade.reopen_lines([given])

    assert _song(ports, given) == (MatchStatus.NEEDS_REVIEW, None, ReasonCode.USER_UNMATCHED, None)
    assert _song(ports, left) == before


def test_delete_matches_of_lines(ports: Ports) -> None:
    name = _name(ports)
    given = _line(ports, name, MatchStatus.AUTO_MATCHED)
    left = _line(ports, name, MatchStatus.AUTO_MATCHED)
    _match(ports, given, _file(ports, "Elsewhere"), score=0.9)
    _match(ports, given, _file(ports, "Elsewhere"), score=0.7)
    _match(ports, left, _file(ports, "Cascade Band"))

    ports.cascade.delete_matches_of_lines([given])

    assert ports.matches.get_by_identity(given) is None
    assert ports.matches.get_by_identity(left) is not None


def test_no_lines_given_writes_nothing(ports: Ports) -> None:
    name = _name(ports)
    line = _line(ports, name, MatchStatus.AUTO_MATCHED)
    _match(ports, line, _file(ports, "Elsewhere"))
    before = _song(ports, line)

    ports.cascade.reject_lines([])
    ports.cascade.reopen_lines([])
    ports.cascade.delete_matches_of_lines([])

    assert _song(ports, line) == before
    assert ports.matches.get_by_identity(line) is not None


# --- the lock (PostgreSQL) -----------------------------------------------------------------------


@pytest.mark.integration
def test_lines_of_with_lock_holds_the_lines(migrated_db: str) -> None:
    """A matcher's write on a locked line waits for the action's transaction."""
    with (
        psycopg.connect(migrated_db, row_factory=dict_row) as action,
        # The timeout is the connection's own, so a rollback never clears it.
        psycopg.connect(
            migrated_db, row_factory=dict_row, options="-c lock_timeout=200ms"
        ) as matcher,
    ):
        ports = _pg_ports(action)
        name = _name(ports)
        line = _line(ports, name, MatchStatus.PENDING, tier=None, reason=None)
        action.commit()

        assert set(_lines(ports, name, lock=True)) == {line}
        with pytest.raises(psycopg.errors.LockNotAvailable):
            matcher.execute(
                "UPDATE track_identities SET match_status = 'auto_matched' WHERE id = %s", (line,)
            )
        matcher.rollback()
        action.rollback()

        # Without the lock, the read holds nothing.
        _lines(ports, name)
        matcher.execute(
            "UPDATE track_identities SET match_status = 'auto_matched' WHERE id = %s", (line,)
        )
        matcher.rollback()
        action.rollback()
