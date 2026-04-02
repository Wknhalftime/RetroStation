from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.log_artists import PgLogArtistRepository
from backend.db.repositories.log_identities import PgLogIdentityRepository
from backend.domain.enums import MatchStatus
from backend.domain.models import LogArtist, LogIdentity


def test_log_artist_upsert_and_conflict(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLogArtistRepository(conn)
        a1 = LogArtist(id=uuid4(), original_name="THE BEATLES", normalized_name="beatles")
        result = repo.upsert(a1)
        assert result.normalized_name == "beatles"

        # Second upsert with same normalized_name returns original row
        a2 = LogArtist(id=uuid4(), original_name="The Beatles", normalized_name="beatles")
        result2 = repo.upsert(a2)
        assert result2.id == result.id  # same row, not a new one
        conn.commit()


def test_log_artist_update_match_status(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLogArtistRepository(conn)
        a = LogArtist(id=uuid4(), original_name="METALLICA", normalized_name="metallica")
        created = repo.upsert(a)
        assert created.match_status == MatchStatus.PENDING

        repo.update_match_status(created.id, MatchStatus.AUTO_MATCHED)
        updated = repo.get_by_id(created.id)
        assert updated is not None
        assert updated.match_status == MatchStatus.AUTO_MATCHED
        conn.commit()


def test_log_artist_update_embedding(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLogArtistRepository(conn)
        a = LogArtist(id=uuid4(), original_name="NIRVANA", normalized_name="nirvana")
        created = repo.upsert(a)
        assert created.embedding is None

        repo.update_embedding(created.id, [0.1] * 1024)
        updated = repo.get_by_id(created.id)
        assert updated is not None
        assert updated.embedding is not None
        assert len(updated.embedding) == 1024
        conn.commit()


def test_log_identity_upsert_and_conflict(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        artist_repo = PgLogArtistRepository(conn)
        identity_repo = PgLogIdentityRepository(conn)

        artist = artist_repo.upsert(
            LogArtist(id=uuid4(), original_name="PEARL JAM", normalized_name="pearl jam")
        )
        i1 = LogIdentity(
            id=uuid4(), artist_id=artist.id,
            original_title="Alive", normalized_title="alive",
            normalized_signature="abc123def456abc123def456abc123de",
        )
        result = identity_repo.upsert(i1)
        assert result.normalized_signature == "abc123def456abc123def456abc123de"

        # Conflict returns existing
        i2 = LogIdentity(
            id=uuid4(), artist_id=artist.id,
            original_title="Alive (Live)", normalized_title="alive",
            normalized_signature="abc123def456abc123def456abc123de",
        )
        result2 = identity_repo.upsert(i2)
        assert result2.id == result.id
        conn.commit()


def test_log_identity_bulk_reject_by_artist(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        artist_repo = PgLogArtistRepository(conn)
        identity_repo = PgLogIdentityRepository(conn)

        artist = artist_repo.upsert(
            LogArtist(id=uuid4(), original_name="UNKNOWN ARTIST",
                      normalized_name="unknown artist test reject")
        )
        for i in range(3):
            identity_repo.upsert(LogIdentity(
                id=uuid4(), artist_id=artist.id,
                original_title=f"Song {i}", normalized_title=f"song {i}",
                normalized_signature=f"reject_test_{i}_{'0' * 19}",
            ))

        identity_repo.bulk_reject_by_artist(artist.id)
        identities = identity_repo.get_for_artist(artist.id)
        assert all(i.match_status == MatchStatus.AUTO_REJECTED for i in identities)
        conn.commit()
