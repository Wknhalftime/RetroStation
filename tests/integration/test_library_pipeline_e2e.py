"""E2E integration test: full library pipeline.

Tests the complete flow:
  ingest KAZR CSV → artist matching → insert library file → identity matching

Verifies that at least one identity gets AUTO_MATCHED when a library file
exists for a matched artist.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_days import PgBroadcastDayRepository
from backend.db.repositories.broadcast_play_events import PgBroadcastPlayEventRepository
from backend.db.repositories.broadcast_playlists import PgBroadcastPlaylistRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.mapping_rules import PgMappingRuleRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.domain.broadcast import BroadcastStation
from backend.domain.catalog import Recording
from backend.domain.enums import EnrichmentStatus
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.artist_matching_service import match_artists_for_playlist
from backend.services.identity_matching_service import match_identities_for_playlist
from backend.services.ingestion_service import ingest_csv
from tests.fakes.mb_client import FakeMbClient

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "KAZR-FakeData.csv"


@pytest.mark.slow
def test_library_pipeline_auto_match(migrated_db: str) -> None:
    """Full pipeline: ingest → artist match → library file → identity match.

    After inserting a library file for the Metallica MBID that matches
    a known identity title from the KAZR fixture, at least one identity
    should be AUTO_MATCHED.
    """
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        # --- Setup ---
        station = PgBroadcastStationRepository(conn).create(BroadcastStation(
            id=uuid4(), call_letters="KAZR-LIB-E2E", name="KAZR Library E2E",
        ))

        # --- Step 1: Ingest CSV ---
        file_bytes = FIXTURE_PATH.read_bytes()
        result = ingest_csv(
            file_bytes=file_bytes,
            file_name="KAZR-LIB-E2E.csv",
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

        assert result.rows_processed >= 3166
        assert result.artists_created >= 100

        # --- Step 2: Artist matching with FakeMbClient ---
        # Seed Metallica so it gets AUTO_MATCHED
        fake_mb = FakeMbClient({
            "METALLICA": [
                {"id": "mbid-metallica-lib", "name": "Metallica",
                 "sort-name": "Metallica", "score": 100},
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

        # Confirm Metallica was matched
        metallica_rows = conn.execute(
            "SELECT la.id FROM broadcast_artists la WHERE la.normalized_name = 'metallica'"
        ).fetchall()
        assert len(metallica_rows) >= 1, "Metallica log_artist not found"
        metallica_log_id = metallica_rows[0]["id"]

        artist_match_row = conn.execute(
            "SELECT target_id FROM matches WHERE artist_id = %s",
            (metallica_log_id,),
        ).fetchone()
        assert artist_match_row is not None, "No match found for Metallica"
        assert artist_match_row["target_id"] == "mbid-metallica-lib"

        # --- Step 3: Find a Metallica identity from the fixture ---
        # Query for an identity belonging to Metallica to get the title
        metallica_identities = conn.execute(
            "SELECT li.original_title FROM track_identities li "
            "WHERE li.broadcast_artist_id = %s LIMIT 5",
            (metallica_log_id,),
        ).fetchall()
        assert len(metallica_identities) >= 1, "No identities found for Metallica"

        # Use the first identity title to create a matching library file
        target_title = metallica_identities[0]["original_title"]

        # --- Step 4: Insert a Recording and library file for that artist/title ---
        # recording_id in library_files is a TEXT FK to recordings.id (an MBID string)
        recording_mbid = "mbid-rec-" + uuid4().hex[:8]
        recording = Recording(
            id=recording_mbid,
            title=target_title,
            needs_enhancement=False,
        )
        PgRecordingRepository(conn).upsert(recording)

        lib_file = LibraryFile(
            id=uuid4(),
            file_path=f"/music/metallica/{target_title.lower().replace(' ', '_')}.flac",
            file_hash="deadbeef" + uuid4().hex[:24],
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
            recording_id=recording_mbid,
            audio=AudioMetadata(
                artist_mbid="mbid-metallica-lib",
                track_title=target_title,
            ),
        )
        PgLibraryFileRepository(conn).upsert(lib_file)
        conn.commit()

        # --- Step 5: Identity matching ---
        work_ids = match_identities_for_playlist(
            playlist_id=playlist_id,
            track_identity_repo=PgBroadcastTrackIdentityRepository(conn),
            broadcast_artist_repo=PgBroadcastArtistRepository(conn),
            match_repo=PgMatchRepository(conn),
            library_file_repo=PgLibraryFileRepository(conn),
            rules_repo=PgMappingRuleRepository(conn),
        )
        conn.commit()

        # --- Verify: at least one identity is AUTO_MATCHED ---
        auto_matched_rows = conn.execute(
            "SELECT count(*) AS cnt FROM track_identities "
            "WHERE match_status = 'auto_matched'"
        ).fetchone()
        assert auto_matched_rows is not None
        assert auto_matched_rows["cnt"] >= 1, (
            "Expected at least one AUTO_MATCHED identity after library file insert"
        )

        # The matched identity's match row should reference the library file
        match_row = conn.execute(
            "SELECT m.library_file_id FROM matches m "
            "JOIN track_identities li ON li.id = m.identity_id "
            "WHERE li.match_status = 'auto_matched' LIMIT 1"
        ).fetchone()
        assert match_row is not None, "No match row found for AUTO_MATCHED identity"
        assert match_row["library_file_id"] == lib_file.id

        # work_ids returned should be non-empty (recording_id of the matched file)
        assert len(work_ids) >= 1
