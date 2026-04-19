from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_days import PgBroadcastDayRepository
from backend.db.repositories.broadcast_play_events import PgBroadcastPlayEventRepository
from backend.db.repositories.broadcast_playlists import PgBroadcastPlaylistRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.domain.broadcast import BroadcastStation
from backend.services.ingestion_service import DuplicatePlaylistError, ingest_csv

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "KAZR-FakeData.csv"


def test_ingest_kazr_csv(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        station_repo = PgBroadcastStationRepository(conn)
        station = station_repo.create(BroadcastStation(
            id=uuid4(), call_letters="KAZR-FM", name="KAZR",
        ))

        file_bytes = FIXTURE_PATH.read_bytes()
        result = ingest_csv(
            file_bytes=file_bytes,
            file_name="KAZR-FakeData.csv",
            station_id=str(station.id),
            playlist_repo=PgBroadcastPlaylistRepository(conn),
            broadcast_artist_repo=PgBroadcastArtistRepository(conn),
            track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
            play_event_repo=PgBroadcastPlayEventRepository(conn),
            broadcast_day_repo=PgBroadcastDayRepository(conn),
        )

        assert result.rows_processed == 3167
        assert result.artists_created >= 100  # ~120 unique artists
        assert result.identities_created >= 300  # ~343 unique identities
        assert result.events_created == 3167
        assert result.broadcast_days_created >= 10  # 13 days

        conn.commit()


def test_ingest_duplicate_csv_raises(migrated_db: str) -> None:
    import pytest

    # Use unique bytes so this test is self-contained regardless of DB state
    # from other tests in the same session.
    unique_bytes = (
        b"Station,Played,Artist,Title,Release Year,Grc\r\n"
        b"KAZR-DUP,2005-03-02 00:01:00,TEST ARTIST,Test Title,2000,G\r\n"
    )

    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        station_repo = PgBroadcastStationRepository(conn)
        station = station_repo.create(BroadcastStation(
            id=uuid4(), call_letters="KAZR-FM-DUP", name="KAZR Dup Test",
        ))

        ingest_csv(
            file_bytes=unique_bytes,
            file_name="dup-test.csv",
            station_id=str(station.id),
            playlist_repo=PgBroadcastPlaylistRepository(conn),
            broadcast_artist_repo=PgBroadcastArtistRepository(conn),
            track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
            play_event_repo=PgBroadcastPlayEventRepository(conn),
            broadcast_day_repo=PgBroadcastDayRepository(conn),
        )

        with pytest.raises(DuplicatePlaylistError, match="already ingested"):
            ingest_csv(
                file_bytes=unique_bytes,
                file_name="dup-test.csv",
                station_id=str(station.id),
                playlist_repo=PgBroadcastPlaylistRepository(conn),
                broadcast_artist_repo=PgBroadcastArtistRepository(conn),
                track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
                play_event_repo=PgBroadcastPlayEventRepository(conn),
                broadcast_day_repo=PgBroadcastDayRepository(conn),
            )
        conn.commit()
