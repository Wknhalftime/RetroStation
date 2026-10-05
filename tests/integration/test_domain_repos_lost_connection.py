"""The broadcast, library, curation, matching and catalog sync Pg repositories on a lost
connection (follow-up to PR #133, confirmed with the G1 coordinator).

Requirements: translate at the repository layer; one base exception per subdomain with typed
subclasses. Each repository behind a ``SyncRepos`` route
raises its own subdomain's storage error, and every one of those is a
``StorageUnavailableError``, so a router or ``bounded_connection`` catches them all with one
clause. The backend is terminated with ``pg_terminate_backend`` from a second connection.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from backend.db.repositories.broadcast_days import PgBroadcastDayRepository
from backend.db.repositories.broadcast_stations import PgBroadcastStationRepository
from backend.db.repositories.broadcast_track_identities import (
    PgBroadcastTrackIdentityRepository,
)
from backend.db.repositories.format_overrides import PgFormatOverrideRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.matches import PgMatchRepository
from backend.db.repositories.missing_files import PgMissingFileListingRepository
from backend.db.repositories.song_masters import PgSongMasterRepository
from backend.db.repositories.works import PgWorkRepository
from backend.db.sync_conn import connect_sync
from backend.domain.broadcast import BroadcastError
from backend.domain.catalog import CatalogError
from backend.domain.curation import CurationError
from backend.domain.library import LibraryError
from backend.domain.matching import MatchingError
from backend.domain.system import StorageUnavailableError

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[Any]


@pytest.fixture
def dead_conn(migrated_db: str) -> Iterator[Conn]:
    """A connection whose backend has been terminated; psycopg has not noticed yet."""
    with connect_sync(migrated_db) as conn:
        with psycopg.connect(migrated_db, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))
        yield conn


CALLS: dict[str, tuple[type[Exception], Callable[[Conn], object]]] = {
    "broadcast_stations": (
        BroadcastError,
        lambda c: PgBroadcastStationRepository(c).get_by_call_letters("WLST"),
    ),
    "broadcast_days": (
        BroadcastError,
        lambda c: PgBroadcastDayRepository(c).get_dates_for_station(uuid4()),
    ),
    "broadcast_track_identities": (
        BroadcastError,
        lambda c: PgBroadcastTrackIdentityRepository(c).get_by_id(uuid4()),
    ),
    "library_files": (LibraryError, lambda c: PgLibraryFileRepository(c).get_by_id(uuid4())),
    "missing_files": (LibraryError, lambda c: PgMissingFileListingRepository(c).list_page(0, 5)),
    "format_overrides": (
        CurationError,
        lambda c: PgFormatOverrideRepository(c).list_by_work("w"),
    ),
    "song_masters": (CurationError, lambda c: PgSongMasterRepository(c).delete_by_work("w")),
    "matches": (MatchingError, lambda c: PgMatchRepository(c).get_by_identity(uuid4())),
    "works": (CatalogError, lambda c: PgWorkRepository(c).get_by_id("w")),
}


@pytest.mark.parametrize(("base", "call"), CALLS.values(), ids=CALLS.keys())
def test_a_lost_connection_is_the_subdomains_storage_error(
    dead_conn: Conn, base: type[Exception], call: Callable[[Conn], object]
) -> None:
    with pytest.raises(StorageUnavailableError) as raised:
        call(dead_conn)
    assert isinstance(raised.value, base)
    assert isinstance(raised.value.__cause__, psycopg.OperationalError)
