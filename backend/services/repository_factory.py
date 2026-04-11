from __future__ import annotations

from typing import Any

import psycopg

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_days import PgBroadcastDayRepository
from backend.db.repositories.format_overrides import PgFormatOverrideRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.library_folders import PgLibraryFolderRepository
from backend.db.repositories.library_quarantine import PgLibraryQuarantineRepository
from backend.db.repositories.mapping_rules import PgMappingRuleRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.db.repositories.mb_cache import PgMusicBrainzCacheRepository
from backend.db.repositories.play_events import PgPlayEventRepository
from backend.db.repositories.playlists import PgPlaylistRepository
from backend.db.repositories.progress_tracking import PgTaskProgressRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.settings import PgSettingsRepository
from backend.db.repositories.song_masters import PgSongMasterRepository
from backend.db.repositories.stations import PgStationRepository
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.repositories.track_identities import PgTrackIdentityRepository
from backend.db.repositories.works import PgWorkRepository


class RepositoryFactory:
    """Instantiate all PG repositories from a single connection."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.stations = PgStationRepository(conn)
        self.playlists = PgPlaylistRepository(conn)
        self.broadcast_artists = PgBroadcastArtistRepository(conn)
        self.track_identities = PgTrackIdentityRepository(conn)
        self.play_events = PgPlayEventRepository(conn)
        self.broadcast_days = PgBroadcastDayRepository(conn)
        self.mb_cache = PgMusicBrainzCacheRepository(conn)
        self.artists = PgArtistRepository(conn)
        self.works = PgWorkRepository(conn)
        self.recordings = PgRecordingRepository(conn)
        self.matches = PgMatchRepository(conn)
        self.mapping_rules = PgMappingRuleRepository(conn)
        self.song_masters = PgSongMasterRepository(conn)
        self.progress_tracking = PgTaskProgressRepository(conn)
        self.library_files = PgLibraryFileRepository(conn)
        self.library_folders = PgLibraryFolderRepository(conn)
        self.library_quarantine = PgLibraryQuarantineRepository(conn)
        self.format_overrides = PgFormatOverrideRepository(conn)
        self.settings = PgSettingsRepository(conn)
        self.system_logs = PgSystemLogRepository(conn)
