from __future__ import annotations

from typing import Any

import psycopg

from backend.db.repositories.broadcast_days import PgBroadcastDayRepository
from backend.db.repositories.log_artists import PgLogArtistRepository
from backend.db.repositories.log_events import PgLogEventRepository
from backend.db.repositories.log_identities import PgLogIdentityRepository
from backend.db.repositories.mb_cache import PgMbCacheRepository
from backend.db.repositories.playlists import PgPlaylistRepository
from backend.db.repositories.stations import PgStationRepository


class RepositoryFactory:
    """Instantiate all PG repositories from a single connection."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.stations = PgStationRepository(conn)
        self.playlists = PgPlaylistRepository(conn)
        self.log_artists = PgLogArtistRepository(conn)
        self.log_identities = PgLogIdentityRepository(conn)
        self.log_events = PgLogEventRepository(conn)
        self.broadcast_days = PgBroadcastDayRepository(conn)
        self.mb_cache = PgMbCacheRepository(conn)
