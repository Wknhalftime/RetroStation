from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_play_events import PgBroadcastPlayEventRepository
from backend.db.repositories.broadcast_playlists import PgBroadcastPlaylistRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.domain.broadcast import (
    BroadcastArtist,
    BroadcastPlayEvent,
    BroadcastPlaylist,
    BroadcastStation,
    BroadcastTrackIdentity,
)
from backend.domain.enums import MatchStatus, MatchTier
from backend.services.matching_reasons import ReasonCode


def test_broadcast_artist_upsert_and_conflict(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgBroadcastArtistRepository(conn)
        a1 = BroadcastArtist(id=uuid4(), original_name="THE BEATLES", normalized_name="beatles")
        result = repo.upsert(a1)
        assert result.normalized_name == "beatles"

        # Second upsert with same normalized_name returns original row
        a2 = BroadcastArtist(id=uuid4(), original_name="The Beatles", normalized_name="beatles")
        result2 = repo.upsert(a2)
        assert result2.id == result.id  # same row, not a new one
        conn.commit()


def test_broadcast_artist_update_match_status(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgBroadcastArtistRepository(conn)
        a = BroadcastArtist(id=uuid4(), original_name="METALLICA", normalized_name="metallica")
        created = repo.upsert(a)
        assert created.match_status == MatchStatus.PENDING

        repo.update_match_status(created.id, MatchStatus.AUTO_MATCHED)
        updated = repo.get_by_id(created.id)
        assert updated is not None
        assert updated.match_status == MatchStatus.AUTO_MATCHED
        conn.commit()


def test_broadcast_artist_update_embedding(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgBroadcastArtistRepository(conn)
        a = BroadcastArtist(id=uuid4(), original_name="NIRVANA", normalized_name="nirvana")
        created = repo.upsert(a)
        assert created.embedding is None

        repo.update_embedding(created.id, [0.1] * 1024)
        updated = repo.get_by_id(created.id)
        assert updated is not None
        assert updated.embedding is not None
        assert len(updated.embedding) == 1024
        conn.commit()


def test_track_identity_upsert_and_conflict(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        artist_repo = PgBroadcastArtistRepository(conn)
        identity_repo = PgBroadcastTrackIdentityRepository(conn)

        artist = artist_repo.upsert(
            BroadcastArtist(id=uuid4(), original_name="PEARL JAM", normalized_name="pearl jam")
        )
        i1 = BroadcastTrackIdentity(
            id=uuid4(), broadcast_artist_id=artist.id,
            original_title="Alive", normalized_title="alive",
            normalized_signature="abc123def456abc123def456abc123de",
        )
        result = identity_repo.upsert(i1)
        assert result.normalized_signature == "abc123def456abc123def456abc123de"

        # Conflict returns existing
        i2 = BroadcastTrackIdentity(
            id=uuid4(), broadcast_artist_id=artist.id,
            original_title="Alive (Live)", normalized_title="alive",
            normalized_signature="abc123def456abc123def456abc123de",
        )
        result2 = identity_repo.upsert(i2)
        assert result2.id == result.id
        conn.commit()


def test_track_identity_bulk_reject_by_artist(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        artist_repo = PgBroadcastArtistRepository(conn)
        identity_repo = PgBroadcastTrackIdentityRepository(conn)

        artist = artist_repo.upsert(
            BroadcastArtist(id=uuid4(), original_name="UNKNOWN ARTIST",
                      normalized_name="unknown artist test reject")
        )
        for i in range(3):
            identity_repo.upsert(BroadcastTrackIdentity(
                id=uuid4(), broadcast_artist_id=artist.id,
                original_title=f"Song {i}", normalized_title=f"song {i}",
                normalized_signature=f"reject_test_{i}_{'0' * 19}",
            ))

        identity_repo.bulk_reject_by_artist(artist.id)
        identities = identity_repo.get_for_artist(artist.id)
        assert all(i.match_status == MatchStatus.AUTO_REJECTED for i in identities)
        conn.commit()


def test_play_event_create_returns_persisted_row_on_conflict(migrated_db: str) -> None:
    """On duplicate natural key, create() must return the row that actually exists.

    Regression for PR #14 review: the returned model's `id` must match the
    first insert's id, not the losing candidate's — otherwise any caller
    that trusts the returned model gets a uuid the DB never stored.
    """
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        station = PgBroadcastStationRepository(conn).create(BroadcastStation(
            id=uuid4(), call_letters="KAZR-CONFLICT", name="KAZR Conflict Test",
        ))
        playlist = PgBroadcastPlaylistRepository(conn).create(BroadcastPlaylist(
            id=uuid4(), name="conflict.csv",
            content_hash="conflict_test_" + "0" * 50,
            station_id=station.id,
        ))
        artist = PgBroadcastArtistRepository(conn).upsert(BroadcastArtist(
            id=uuid4(),
            original_name="CONFLICT ARTIST",
            normalized_name="conflict artist test",
        ))
        identity = PgBroadcastTrackIdentityRepository(conn).upsert(BroadcastTrackIdentity(
            id=uuid4(), broadcast_artist_id=artist.id,
            original_title="Conflict Song", normalized_title="conflict song",
            normalized_signature="conflict_sig_" + "0" * 19,
        ))
        played_at = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
        repo = PgBroadcastPlayEventRepository(conn)

        first = repo.create(BroadcastPlayEvent(
            id=uuid4(), identity_id=identity.id, playlist_id=playlist.id,
            played_at=played_at,
        ))

        duplicate_candidate_id = uuid4()
        assert duplicate_candidate_id != first.id
        second = repo.create(BroadcastPlayEvent(
            id=duplicate_candidate_id, identity_id=identity.id,
            playlist_id=playlist.id, played_at=played_at,
        ))

        # Contract: returned row reflects what is persisted, not the caller's
        # losing candidate.
        assert second.id == first.id
        assert second.id != duplicate_candidate_id

        # And the DB really only has one row for this natural key.
        count = conn.execute(
            """SELECT count(*) AS n FROM play_events
               WHERE identity_id = %s AND playlist_id = %s AND played_at = %s""",
            (identity.id, playlist.id, played_at),
        ).fetchone()
        assert count is not None
        assert count["n"] == 1
        conn.commit()


def test_broadcast_artist_update_match_status_persists_reason_columns(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgBroadcastArtistRepository(conn)
        a = BroadcastArtist(id=uuid4(), original_name="PRINCE", normalized_name="prince")
        created = repo.upsert(a)
        repo.update_match_status(
            created.id,
            MatchStatus.NEEDS_REVIEW,
            reason_code=ReasonCode.LOW_CONFIDENCE,
            reason_detail="Score 65% — below confidence threshold",
        )
        cur = conn.execute(
            "SELECT reason_code, reason_detail FROM broadcast_artists WHERE id = %s",
            (created.id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row["reason_code"] == "LOW_CONFIDENCE"
        assert row["reason_detail"] == "Score 65% — below confidence threshold"
        conn.commit()


def test_broadcast_artist_update_match_status_clears_reason_when_not_passed(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgBroadcastArtistRepository(conn)
        a = BroadcastArtist(id=uuid4(), original_name="MADONNA", normalized_name="madonna")
        created = repo.upsert(a)
        # First: set a reason
        repo.update_match_status(
            created.id,
            MatchStatus.NEEDS_REVIEW,
            reason_code=ReasonCode.LOW_CONFIDENCE,
            reason_detail="Score 65%",
        )
        # Second: transition without reason — should CLEAR the stale reason
        repo.update_match_status(created.id, MatchStatus.AUTO_MATCHED)
        cur = conn.execute(
            "SELECT reason_code, reason_detail FROM broadcast_artists WHERE id = %s",
            (created.id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row["reason_code"] is None
        assert row["reason_detail"] is None
        conn.commit()


def test_track_identity_update_match_status_persists_reason_columns(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        artist_repo = PgBroadcastArtistRepository(conn)
        identity_repo = PgBroadcastTrackIdentityRepository(conn)
        artist = artist_repo.upsert(
            BroadcastArtist(id=uuid4(), original_name="BOWIE", normalized_name="bowie")
        )
        identity = identity_repo.upsert(BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title="Heroes",
            normalized_title="heroes",
            normalized_signature="bowie00heroes00bowie00heroes0000",
        ))
        identity_repo.update_match_status(
            identity.id,
            MatchStatus.NEEDS_REVIEW,
            tier=None,
            reason_code=ReasonCode.NO_LOCAL_FILES,
            reason_detail="Artist MBID confirmed but no matching local recording found",
        )
        cur = conn.execute(
            "SELECT reason_code, reason_detail FROM track_identities WHERE id = %s",
            (identity.id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row["reason_code"] == "NO_LOCAL_FILES"
        assert row["reason_detail"] == "Artist MBID confirmed but no matching local recording found"
        conn.commit()


def test_track_identity_update_match_status_clears_reason_when_not_passed(
    migrated_db: str,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        artist_repo = PgBroadcastArtistRepository(conn)
        identity_repo = PgBroadcastTrackIdentityRepository(conn)
        artist = artist_repo.upsert(
            BroadcastArtist(
                id=uuid4(), original_name="KATE BUSH", normalized_name="kate bush"
            )
        )
        identity = identity_repo.upsert(BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title="Running Up That Hill",
            normalized_title="running up that hill",
            normalized_signature="katebush0running0katebush0runnin",
        ))
        identity_repo.update_match_status(
            identity.id,
            MatchStatus.NEEDS_REVIEW,
            tier=None,
            reason_code=ReasonCode.LOW_CONFIDENCE,
            reason_detail="Score 72%",
        )
        identity_repo.update_match_status(
            identity.id,
            MatchStatus.AUTO_MATCHED,
            tier=MatchTier.MUSICBRAINZ_ID_EXACT,
        )
        cur = conn.execute(
            "SELECT reason_code, reason_detail FROM track_identities WHERE id = %s",
            (identity.id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row["reason_code"] is None
        assert row["reason_detail"] is None
        conn.commit()
