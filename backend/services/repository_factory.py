from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
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
from backend.db.repositories.missing_files import PgMissingFileListingRepository
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.repositories.play_file_resolution import PgPlayFileResolutionRepository
from backend.db.repositories.playable_schedule import PgPlayableScheduleRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.song_masters import PgSongMasterRepository
from backend.db.repositories.stream_cues import PgStreamCueRepository
from backend.db.repositories.system_logs import PgSystemLogRepository
from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.db.repositories.user_settings import PgUserSettingRepository
from backend.db.repositories.works import PgWorkRepository
from backend.db.sync_conn import connect_sync
from backend.services.identity_resolution_service import RecalcRepos
from backend.services.m3u_generator_service import M3uRepos
from backend.services.missing_file_reconciliation_service import ReconciliationRepos


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
    missing_files: PgMissingFileListingRepository


@dataclass
class CatalogRepos:
    """Catalog-domain repositories (artists, works, recordings, masters).

    ``play_file_resolution`` reads curation's view of which file plays for a play (D17).
    """

    artists: PgArtistRepository
    works: PgWorkRepository
    recordings: PgRecordingRepository
    matches: PgMatchRepository
    song_masters: PgSongMasterRepository
    play_file_resolution: PgPlayFileResolutionRepository


@dataclass
class SystemRepos:
    """System/infrastructure repositories."""

    mapping_rules: PgMappingRuleRepository
    task_progress: PgTaskProgressRepository
    musicbrainz_cache: PgMusicBrainzCacheRepository
    user_settings: PgUserSettingRepository
    system_logs: PgSystemLogRepository


@dataclass
class StreamingRepos:
    """Tune-in streaming repositories: the cue cache and the playable schedule reader.

    The reader checks no cue freshness (D20): a cue row for the final file's audio is used as
    it is, whatever its ``analyser_version``.
    """

    cues: PgStreamCueRepository
    schedule: PgPlayableScheduleRepository


class RepositoryFactory:
    """Instantiate all PG repositories from a single connection.

    Provides grouped sub-factories (``repos.broadcast``, ``repos.library``,
    ``repos.catalog``, ``repos.system``, ``repos.streaming``) and flat attribute access for
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
            missing_files=PgMissingFileListingRepository(conn),
        )
        self.catalog = CatalogRepos(
            artists=PgArtistRepository(conn),
            works=PgWorkRepository(conn),
            recordings=PgRecordingRepository(conn),
            matches=PgMatchRepository(conn),
            song_masters=PgSongMasterRepository(conn),
            play_file_resolution=PgPlayFileResolutionRepository(conn),
        )
        self.system = SystemRepos(
            mapping_rules=PgMappingRuleRepository(conn),
            task_progress=PgTaskProgressRepository(conn),
            musicbrainz_cache=PgMusicBrainzCacheRepository(conn),
            user_settings=PgUserSettingRepository(conn),
            system_logs=PgSystemLogRepository(conn),
        )
        self.streaming = StreamingRepos(
            cues=PgStreamCueRepository(conn),
            schedule=PgPlayableScheduleRepository(conn),
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
        self.play_file_resolution = self.catalog.play_file_resolution
        self.library_files = self.library.files
        self.library_folders = self.library.folders
        self.library_quarantine = self.library.quarantine
        self.format_overrides = self.library.format_overrides
        self.missing_files = self.library.missing_files
        self.mapping_rules = self.system.mapping_rules
        self.task_progress = self.system.task_progress
        self.musicbrainz_cache = self.system.musicbrainz_cache
        self.user_settings = self.system.user_settings
        self.system_logs = self.system.system_logs

    def m3u_repos(self) -> M3uRepos:
        """The repositories both M3U exports read."""
        return M3uRepos(
            track_identities=self.broadcast.identities,
            play_file_resolution=self.catalog.play_file_resolution,
            library_files=self.library.files,
            user_settings=self.system.user_settings,
        )


@contextmanager
def recalc_repos(db_url: str) -> Iterator[RecalcRepos]:
    """Open a sync connection scoped to one manual-resolve master recalc.

    Composition root for ``identity_resolution_service.recalculate_for_work_sync``'s
    ``repos_factory`` argument (AUD-054): builds the concrete Pg adapters and
    hands back the ``RecalcRepos`` port bundle the service depends on, so
    that module never imports ``backend.db`` itself.
    """
    with connect_sync(db_url) as conn:
        yield RecalcRepos(
            song_masters=PgSongMasterRepository(conn),
            recordings=PgRecordingRepository(conn),
            library_files=PgLibraryFileRepository(conn),
            commit=conn.commit,
        )


def reconciliation_repos(repos: RepositoryFactory) -> ReconciliationRepos:
    """The port bundle missing-file folds and deletes write through, on *repos*' connection.

    Composition root, beside recalc_repos (AUD-054): the scan task and the Missing
    Files endpoints build it here instead of each wiring it by hand.
    """
    return ReconciliationRepos(
        files=repos.library_files,
        matches=repos.matches,
        works=repos.works,
        song_masters=repos.song_masters,
        format_overrides=repos.format_overrides,
    )
