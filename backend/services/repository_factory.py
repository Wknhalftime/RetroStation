from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.rows import DictRow

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
from backend.db.repositories.broadcast_days import PgBroadcastDayRepository
from backend.db.repositories.broadcast_play_events import PgBroadcastPlayEventRepository
from backend.db.repositories.broadcast_playlists import PgBroadcastPlaylistRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.db.repositories.format_overrides import PgFormatOverrideRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.library_folders import PgLibraryFolderRepository
from backend.db.repositories.library_quarantine import PgLibraryQuarantineRepository
from backend.db.repositories.mapping_rules import PgMappingRuleRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.song_masters import PgSongMasterRepository
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.repositories.user_settings import PgUserSettingRepository
from backend.db.repositories.works import PgWorkRepository


@dataclass
class BroadcastRepos:
    """Broadcast-domain repositories."""

    stations: PgBroadcastStationRepository
    playlists: PgBroadcastPlaylistRepository
    artists: PgBroadcastArtistRepository
    identities: PgBroadcastTrackIdentityRepository
    events: PgBroadcastPlayEventRepository
    days: PgBroadcastDayRepository


@dataclass
class LibraryRepos:
    """Library-domain repositories."""

    files: PgLibraryFileRepository
    folders: PgLibraryFolderRepository
    quarantine: PgLibraryQuarantineRepository
    format_overrides: PgFormatOverrideRepository


@dataclass
class CatalogRepos:
    """Catalog-domain repositories (artists, works, recordings, masters)."""

    artists: PgArtistRepository
    works: PgWorkRepository
    recordings: PgRecordingRepository
    matches: PgMatchRepository
    song_masters: PgSongMasterRepository


@dataclass
class SystemRepos:
    """System/infrastructure repositories."""

    mapping_rules: PgMappingRuleRepository
    task_progress: PgTaskProgressRepository
    musicbrainz_cache: PgMusicBrainzCacheRepository
    user_settings: PgUserSettingRepository
    system_logs: PgSystemLogRepository


class RepositoryFactory:
    """Instantiate all PG repositories from a single connection.

    Provides grouped sub-factories (``repos.broadcast``, ``repos.library``,
    ``repos.catalog``, ``repos.system``) and flat attribute access for
    backwards compatibility with existing task and router code.
    """

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self.broadcast = BroadcastRepos(
            stations=PgBroadcastStationRepository(conn),
            playlists=PgBroadcastPlaylistRepository(conn),
            artists=PgBroadcastArtistRepository(conn),
            identities=PgBroadcastTrackIdentityRepository(conn),
            events=PgBroadcastPlayEventRepository(conn),
            days=PgBroadcastDayRepository(conn),
        )
        self.library = LibraryRepos(
            files=PgLibraryFileRepository(conn),
            folders=PgLibraryFolderRepository(conn),
            quarantine=PgLibraryQuarantineRepository(conn),
            format_overrides=PgFormatOverrideRepository(conn),
        )
        self.catalog = CatalogRepos(
            artists=PgArtistRepository(conn),
            works=PgWorkRepository(conn),
            recordings=PgRecordingRepository(conn),
            matches=PgMatchRepository(conn),
            song_masters=PgSongMasterRepository(conn),
        )
        self.system = SystemRepos(
            mapping_rules=PgMappingRuleRepository(conn),
            task_progress=PgTaskProgressRepository(conn),
            musicbrainz_cache=PgMusicBrainzCacheRepository(conn),
            user_settings=PgUserSettingRepository(conn),
            system_logs=PgSystemLogRepository(conn),
        )

        # Flat access — delegates to sub-factories for backwards compatibility
        self.broadcast_stations = self.broadcast.stations
        self.broadcast_playlists = self.broadcast.playlists
        self.broadcast_artists = self.broadcast.artists
        self.broadcast_identities = self.broadcast.identities
        self.broadcast_events = self.broadcast.events
        self.broadcast_days = self.broadcast.days
        self.artists = self.catalog.artists
        self.works = self.catalog.works
        self.recordings = self.catalog.recordings
        self.matches = self.catalog.matches
        self.song_masters = self.catalog.song_masters
        self.library_files = self.library.files
        self.library_folders = self.library.folders
        self.library_quarantine = self.library.quarantine
        self.format_overrides = self.library.format_overrides
        self.mapping_rules = self.system.mapping_rules
        self.task_progress = self.system.task_progress
        self.musicbrainz_cache = self.system.musicbrainz_cache
        self.user_settings = self.system.user_settings
        self.system_logs = self.system.system_logs
