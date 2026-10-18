"""get_pending_for_playlist against PostgreSQL: the songs a matching stage works on.

Each pending song of the playlist comes back once, however often it was played, and without
its embedding: matching never reads the vector (AUD-R020), and loading it cost a sort of the
1024-dim column plus a Python parse per song (profile 2026-10-18).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_play_events import PgBroadcastPlayEventRepository
from backend.db.repositories.broadcast_playlists import PgBroadcastPlaylistRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.broadcast_track_identities import (
    PgBroadcastTrackIdentityRepository,
)
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import MatchStatus, MatchTier

_START = datetime(2001, 3, 15, 12, 0, tzinfo=UTC)


def _song(conn: psycopg.Connection[Any], title: str) -> BroadcastTrackIdentity:
    artist = PgBroadcastArtistRepository(conn).upsert(
        BroadcastArtist(id=uuid4(), original_name="ABBA", normalized_name=f"abba-{uuid4().hex}")
    )
    return PgBroadcastTrackIdentityRepository(conn).upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title=title,
            normalized_title=title.lower(),
            normalized_signature=f"abba-{title.lower()}-{uuid4().hex}",
        )
    )


def _playlist(conn: psycopg.Connection[Any]) -> UUID:
    station = PgBroadcastStationRepository(conn).create(
        BroadcastStation(id=uuid4(), call_letters=f"KPS-{uuid4().hex[:8]}", name="Pending")
    )
    playlist = PgBroadcastPlaylistRepository(conn).create(
        BroadcastPlaylist(
            id=uuid4(), name="pending.csv", content_hash=uuid4().hex, station_id=station.id
        )
    )
    return playlist.id


def _play(conn: psycopg.Connection[Any], playlist_id: UUID, song_id: UUID, minute: int) -> None:
    PgBroadcastPlayEventRepository(conn).create(
        BroadcastPlayEvent(
            id=uuid4(),
            identity_id=song_id,
            playlist_id=playlist_id,
            played_at=_START + timedelta(minutes=minute),
        )
    )


def test_a_song_played_several_times_comes_back_once(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        playlist_id, song = _playlist(conn), _song(conn, "Waterloo")
        for minute in range(3):
            _play(conn, playlist_id, song.id, minute)

        pending = PgBroadcastTrackIdentityRepository(conn).get_pending_for_playlist(playlist_id)

        assert [p.id for p in pending] == [song.id]


def test_only_this_playlists_pending_songs_come_back(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgBroadcastTrackIdentityRepository(conn)
        playlist_id, other_playlist_id = _playlist(conn), _playlist(conn)
        wanted, decided, elsewhere = (
            _song(conn, "Waterloo"),
            _song(conn, "Fernando"),
            _song(conn, "Chiquitita"),
        )
        _play(conn, playlist_id, wanted.id, 0)
        _play(conn, playlist_id, decided.id, 1)
        _play(conn, other_playlist_id, elsewhere.id, 2)
        _play(conn, other_playlist_id, wanted.id, 3)
        repo.update_match_status(decided.id, MatchStatus.AUTO_MATCHED, MatchTier.LOCAL_FILE_FUZZY)

        pending = repo.get_pending_for_playlist(playlist_id)

        assert [p.id for p in pending] == [wanted.id]


def test_a_pending_song_comes_back_whole_but_without_its_embedding(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgBroadcastTrackIdentityRepository(conn)
        playlist_id, song = _playlist(conn), _song(conn, "Waterloo")
        _play(conn, playlist_id, song.id, 0)
        repo.update_embedding(song.id, [0.5] * 1024)
        stored = repo.get_by_id(song.id)
        assert stored is not None and stored.embedding is not None

        (pending,) = repo.get_pending_for_playlist(playlist_id)

        assert pending.embedding is None
        assert pending == BroadcastTrackIdentity(**{**vars(stored), "embedding": None})
