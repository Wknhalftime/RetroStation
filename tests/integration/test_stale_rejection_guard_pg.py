"""The song guarded write against PostgreSQL also checks the rejected files the worker read.

``update_match_status_if_pending(..., rejected_file_ids_read=...)`` writes only while the row is
PENDING and still holds exactly those rejected files, compared as a set: the API stores them
with ``array_agg(DISTINCT ...)``, which may reorder them. The unit-level race is
tests/services/test_stale_rejection_read.py; the model is docs/tla/MatchingStatus.tla.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, MatchTier


def _unique(name: str) -> str:
    return f"{name}-{uuid4().hex}"


def _song(conn: psycopg.Connection[dict[str, object]], rejected: list[UUID]) -> UUID:
    artist = PgBroadcastArtistRepository(conn).upsert(
        BroadcastArtist(id=uuid4(), original_name="DEVO", normalized_name=_unique("devo"))
    )
    song = PgBroadcastTrackIdentityRepository(conn).upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title="Whip It",
            normalized_title="whip it",
            normalized_signature=_unique("devo00whipit"),
        )
    )
    conn.execute(
        "UPDATE track_identities SET rejected_file_ids = %s::uuid[] WHERE id = %s",
        (rejected, song.id),
    )
    return song.id


def _status(conn: psycopg.Connection[dict[str, object]], song_id: UUID) -> object:
    row = conn.execute(
        "SELECT match_status FROM track_identities WHERE id = %s", (song_id,)
    ).fetchone()
    assert row is not None
    return row["match_status"]


def test_writes_when_the_rejected_files_are_unchanged(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        song_id = _song(conn, [])

        wrote = PgBroadcastTrackIdentityRepository(conn).update_match_status_if_pending(
            song_id, MatchStatus.AUTO_MATCHED, MatchTier.NORMALIZATION, rejected_file_ids_read=()
        )

        assert wrote is True
        assert _status(conn, song_id) == MatchStatus.AUTO_MATCHED.value


def test_skips_a_song_rejected_since_the_read(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        song_id = _song(conn, [uuid4()])  # the curator rejected a file after the worker read

        wrote = PgBroadcastTrackIdentityRepository(conn).update_match_status_if_pending(
            song_id, MatchStatus.AUTO_MATCHED, MatchTier.NORMALIZATION, rejected_file_ids_read=()
        )

        assert wrote is False
        assert _status(conn, song_id) == MatchStatus.PENDING.value


def test_the_same_files_in_another_order_still_write(migrated_db: str) -> None:
    first, second = uuid4(), uuid4()
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        song_id = _song(conn, [second, first])

        wrote = PgBroadcastTrackIdentityRepository(conn).update_match_status_if_pending(
            song_id,
            MatchStatus.NEEDS_REVIEW,
            MatchTier.LOCAL_FILE_FUZZY,
            rejected_file_ids_read=(first, second),
        )

        assert wrote is True
        assert _status(conn, song_id) == MatchStatus.NEEDS_REVIEW.value


def test_without_a_read_the_rejected_files_are_not_checked(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        song_id = _song(conn, [uuid4()])

        wrote = PgBroadcastTrackIdentityRepository(conn).update_match_status_if_pending(
            song_id, MatchStatus.AUTO_REJECTED, MatchTier.UNCLASSIFIED
        )

        assert wrote is True
        assert _status(conn, song_id) == MatchStatus.AUTO_REJECTED.value
