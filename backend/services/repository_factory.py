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
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.repositories.broadcast_play_events import PgBroadcastPlayEventRepository
from backend.db.repositories.broadcast_playlists import PgBroadcastPlaylistRepository
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.song_masters import PgSongMasterRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.db.repositories.user_settings import PgUserSettingRepository
from backend.db.repositories.works import PgWorkRepository


class RepositoryFactory:
    """Instantiate all PG repositories from a single connection."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.stations = PgBroadcastStationRepository(conn)
        self.playlists = PgBroadcastPlaylistRepository(conn)
        self.broadcast_artists = PgBroadcastArtistRepository(conn)
        self.track_identities = PgBroadcastTrackIdentityRepository(conn)
        self.play_events = PgBroadcastPlayEventRepository(conn)
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
        self.settings: PgUserSettingRepository = PgUserSettingRepository(conn)
        self.system_logs = PgSystemLogRepository(conn)
