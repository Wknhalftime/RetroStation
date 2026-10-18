"""PG: migration 0042 drops the HNSW indexes on the song and artist embeddings (2026-10-18).

No query reads a vector (AUD-R020; both indexes had 0 scans on dev). Every match_status write
is a non-HOT update, because match_status is indexed, so each one also inserted into the HNSW
index. On the bench clone that was 2.29 ms per song status write, against 0.026 ms without the
index. The vectors stay in place: only the indexes go. The rollback in backend/db/ recreates
them. Migration rules: no BEGIN/COMMIT, the rollback lives in backend/db/.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_track_identities import (
    PgBroadcastTrackIdentityRepository,
)
from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity

_DB = Path(__file__).resolve().parents[2] / "backend" / "db"
_ROLLBACK = _DB / "rollback_0042_drop_embedding_hnsw.sql"
_VERSION = "0042_drop_embedding_hnsw"
_TABLES = ("track_identities", "broadcast_artists")

type Conn = psycopg.Connection[dict[str, object]]


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    """One transaction, rolled back at the end: the schema is left as migrated."""
    connection = psycopg.connect(migrated_db, row_factory=dict_row)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _hnsw_indexes(conn: Conn) -> dict[str, str]:
    """Table name -> index name, for every HNSW index on the two tables."""
    rows = conn.execute(
        """SELECT tablename, indexname FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = ANY(%s)
              AND indexdef ILIKE '%%USING hnsw%%'""",
        (list(_TABLES),),
    ).fetchall()
    return {str(r["tablename"]): str(r["indexname"]) for r in rows}


def _applied(conn: Conn) -> bool:
    row = conn.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (_VERSION,))
    return row.fetchone() is not None


def test_the_migration_leaves_no_hnsw_index_on_songs_or_artists(conn: Conn) -> None:
    assert _applied(conn)
    assert _hnsw_indexes(conn) == {}


def test_the_vectors_are_still_stored_and_read(conn: Conn) -> None:
    artists = PgBroadcastArtistRepository(conn)
    songs = PgBroadcastTrackIdentityRepository(conn)
    artist = artists.upsert(
        BroadcastArtist(id=uuid4(), original_name="ABBA", normalized_name=f"abba-{uuid4().hex}")
    )
    song = songs.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title="Waterloo",
            normalized_title="waterloo",
            normalized_signature=f"abba-waterloo-{uuid4().hex}",
        )
    )

    artists.update_embedding(artist.id, [0.25] * 1024)
    songs.update_embedding(song.id, [0.5] * 1024)

    stored_artist, stored_song = artists.get_by_id(artist.id), songs.get_by_id(song.id)
    assert stored_artist is not None and stored_artist.embedding == [0.25] * 1024
    assert stored_song is not None and stored_song.embedding == [0.5] * 1024


def test_the_rollback_recreates_both_indexes(conn: Conn) -> None:
    conn.execute(_ROLLBACK.read_text(encoding="utf-8"))

    assert _hnsw_indexes(conn) == {
        "track_identities": "idx_track_identities_embedding",
        "broadcast_artists": "idx_broadcast_artists_embedding",
    }
    assert not _applied(conn)
