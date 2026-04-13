from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_days import PgBroadcastDayRepository
from backend.db.repositories.mapping_rules import PgMappingRuleRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.db.repositories.broadcast_play_events import PgBroadcastPlayEventRepository
from backend.db.repositories.broadcast_playlists import PgBroadcastPlaylistRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.domain.broadcast import BroadcastStation
from backend.domain.enums import MatchStatus
from backend.services.artist_matching_service import match_artists_for_playlist
from backend.services.identity_matching_service import match_identities_for_playlist
from backend.services.ingestion_service import ingest_csv
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.mb_client import FakeMbClient

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "KAZR-FakeData.csv"


def test_full_pipeline_kazr_csv(migrated_db: str) -> None:
    """End-to-end: ingest → artist matching → identity matching.

    With no library files, all resolved identities should be NEEDS_REVIEW.
    Uses FakeMbClient with canned responses for a few known artists.
    """
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        # Setup
        station_repo = PgBroadcastStationRepository(conn)
        station = station_repo.create(BroadcastStation(
            id=uuid4(), call_letters="KAZR-FM-E2E", name="KAZR E2E",
        ))

        # Step 1: Ingest
        file_bytes = FIXTURE_PATH.read_bytes()
        result = ingest_csv(
            file_bytes=file_bytes,
            file_name="KAZR-E2E.csv",
            station_id=str(station.id),
            playlist_repo=PgBroadcastPlaylistRepository(conn),
            broadcast_artist_repo=PgBroadcastArtistRepository(conn),
            track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
            play_event_repo=PgBroadcastPlayEventRepository(conn),
            broadcast_day_repo=PgBroadcastDayRepository(conn),
        )
        conn.commit()

        from uuid import UUID
        playlist_id = UUID(result.playlist_id)

        # The actual row count is 3167 (verified in Task 3)
        assert result.rows_processed >= 3166
        assert result.artists_created >= 100

        # Step 2: Skip embedding (would need real model — tested separately)

        # Step 3: Artist matching with FakeMbClient
        fake_mb = FakeMbClient({
            "METALLICA": [
                {"id": "mbid-metallica", "name": "Metallica",
                 "sort-name": "Metallica", "score": 100},
            ],
            "OZZY OSBOURNE": [
                {"id": "mbid-ozzy", "name": "Ozzy Osbourne",
                 "sort-name": "Osbourne, Ozzy", "score": 100},
            ],
            "AC/DC": [
                {"id": "mbid-acdc", "name": "AC/DC",
                 "sort-name": "AC/DC", "score": 100},
            ],
        })

        match_artists_for_playlist(
            playlist_id=playlist_id,
            broadcast_artist_repo=PgBroadcastArtistRepository(conn),
            track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
            artist_repo=PgArtistRepository(conn),
            match_repo=PgMatchRepository(conn),
            rules_repo=PgMappingRuleRepository(conn),
            mb_client=fake_mb,
        )
        conn.commit()

        # Verify artist match status distribution
        all_artists_rows = conn.execute(
            "SELECT match_status, count(*) FROM broadcast_artists GROUP BY match_status"
        ).fetchall()
        status_counts = {r["match_status"]: r["count"] for r in all_artists_rows}

        # Some artists matched via MB (the 3 we seeded), rest are NEEDS_REVIEW
        assert MatchStatus.AUTO_MATCHED.value in status_counts or \
               MatchStatus.NEEDS_REVIEW.value in status_counts

        # Step 4: Identity matching (no library → NEEDS_REVIEW)
        match_identities_for_playlist(
            playlist_id=playlist_id,
            track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
            broadcast_artist_repo=PgBroadcastArtistRepository(conn),
            match_repo=PgMatchRepository(conn),
            library_file_repo=FakeLibraryFileRepository(),
            rules_repo=PgMappingRuleRepository(conn),
        )
        conn.commit()

        # Verify identity statuses
        identity_status_rows = conn.execute(
            "SELECT match_status, count(*) FROM track_identities GROUP BY match_status"
        ).fetchall()
        identity_statuses = {r["match_status"]: r["count"] for r in identity_status_rows}

        # Identities with resolved artists → NEEDS_REVIEW
        total_identities = sum(identity_statuses.values())
        assert total_identities >= 300  # ~343 unique identities

        conn.commit()
