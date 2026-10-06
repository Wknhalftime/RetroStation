"""AUD-R022 against PostgreSQL: the rejected_file_ids column, its mapper, the batch file lookup and
file merges carrying rejections over (spec 2026-10-05 §4.1)."""

from __future__ import annotations

from datetime import UTC, datetime
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
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import EnrichmentStatus
from backend.domain.library import AudioMetadata, LibraryFile


def _identity(conn: psycopg.Connection[Any]) -> BroadcastTrackIdentity:
    artist = PgBroadcastArtistRepository(conn).upsert(
        BroadcastArtist(id=uuid4(), original_name="ABBA", normalized_name=f"abba-{uuid4().hex}")
    )
    return PgBroadcastTrackIdentityRepository(conn).upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title="Waterloo",
            normalized_title="waterloo",
            normalized_signature=f"abba-waterloo-{uuid4().hex}",
        )
    )


def _file(conn: psycopg.Connection[Any]) -> LibraryFile:
    lf = LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4().hex}.flac",
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(track_title="Waterloo"),
    )
    PgLibraryFileRepository(conn).upsert(lf)
    stored = PgLibraryFileRepository(conn).get_by_path(lf.file_path)
    assert stored is not None
    return stored


def _set_rejections(conn: psycopg.Connection[Any], identity_id: UUID, ids: list[UUID]) -> None:
    conn.execute(
        "UPDATE track_identities SET rejected_file_ids = %s WHERE id = %s",
        (ids, identity_id),
    )


def _rejections(conn: psycopg.Connection[Any], identity_id: UUID) -> tuple[UUID, ...]:
    stored = PgBroadcastTrackIdentityRepository(conn).get_by_id(identity_id)
    assert stored is not None
    return stored.rejected_file_ids


def test_a_new_song_has_no_rejections(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        assert _rejections(conn, _identity(conn).id) == ()


def test_the_mapper_reads_rejections_as_a_tuple(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        song, ids = _identity(conn), [uuid4(), uuid4()]
        _set_rejections(conn, song.id, ids)
        assert _rejections(conn, song.id) == tuple(ids)


def test_get_pending_for_playlist_returns_the_stored_rejections(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        song, ids = _identity(conn), [uuid4(), uuid4()]
        station = PgBroadcastStationRepository(conn).create(
            BroadcastStation(id=uuid4(), call_letters=f"KRJ-{uuid4().hex[:8]}", name="Rejections")
        )
        playlist = PgBroadcastPlaylistRepository(conn).create(
            BroadcastPlaylist(
                id=uuid4(), name="rej.csv", content_hash=uuid4().hex, station_id=station.id
            )
        )
        PgBroadcastPlayEventRepository(conn).create(
            BroadcastPlayEvent(
                id=uuid4(),
                identity_id=song.id,
                playlist_id=playlist.id,
                played_at=datetime(2001, 3, 15, 12, 0, tzinfo=UTC),
            )
        )
        _set_rejections(conn, song.id, ids)

        pending = PgBroadcastTrackIdentityRepository(conn).get_pending_for_playlist(playlist.id)

        assert [p.id for p in pending] == [song.id]
        assert pending[0].rejected_file_ids == tuple(ids)


def test_get_by_ids_returns_present_and_missing_files_and_omits_unknown(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        present, missing = _file(conn), _file(conn)
        conn.execute(
            "UPDATE library_files SET file_status = 'missing' WHERE id = %s",
            (missing.id,),
        )
        found = PgLibraryFileRepository(conn).get_by_ids([present.id, missing.id, uuid4()])
        assert {f.id for f in found} == {present.id, missing.id}


def test_get_by_ids_with_no_ids_is_empty(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        assert PgLibraryFileRepository(conn).get_by_ids([]) == []


def test_merge_into_repoints_a_rejection_to_the_surviving_file(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        source, target, other = _file(conn), _file(conn), uuid4()
        song = _identity(conn)
        _set_rejections(conn, song.id, [source.id, other])

        PgLibraryFileRepository(conn).merge_into(source.id, target.id)

        assert set(_rejections(conn, song.id)) == {target.id, other}


def test_merge_into_does_not_duplicate_a_rejection(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        source, target = _file(conn), _file(conn)
        song = _identity(conn)
        _set_rejections(conn, song.id, [source.id, target.id])

        PgLibraryFileRepository(conn).merge_into(source.id, target.id)

        assert _rejections(conn, song.id) == (target.id,)


def test_merge_into_leaves_unrelated_songs_alone(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        source, target, unrelated = _file(conn), _file(conn), uuid4()
        song = _identity(conn)
        _set_rejections(conn, song.id, [unrelated])

        PgLibraryFileRepository(conn).merge_into(source.id, target.id)

        assert _rejections(conn, song.id) == (unrelated,)
