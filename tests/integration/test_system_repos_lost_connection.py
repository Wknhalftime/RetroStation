"""The system subdomain's sync Pg repositories when their database connection is lost.

Requirements: a low-level exception is handled at its layer (the repository translates
psycopg's ``OperationalError`` into a domain error), and the G1 final review (a lost
connection in a settings read or upsert surfaced as a 500). The request's backend is
terminated with ``pg_terminate_backend`` from a second connection, so the repository meets a
dead connection as it would in production.

Only a *lost* connection is translated: an ``OperationalError`` on a live connection (a lock
wait past ``lock_timeout``, a deadlock) keeps its psycopg type, which the retry and per-item
handlers elsewhere rely on.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.db.repositories.user_settings import PgUserSettingRepository
from backend.db.sync_conn import connect_sync
from backend.domain.system import MusicBrainzCache, StorageUnavailableError, UserSetting

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[Any]


def _terminate(dsn: str, conn: Conn) -> None:
    """End ``conn``'s backend from a second connection, as a server restart would."""
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))


@pytest.fixture
def dead_conn(migrated_db: str) -> Iterator[Conn]:
    """A connection whose backend has been terminated; psycopg has not noticed yet."""
    with connect_sync(migrated_db) as conn:
        _terminate(migrated_db, conn)
        yield conn


def _cache_row() -> MusicBrainzCache:
    now = datetime.now(UTC)
    return MusicBrainzCache(
        id=uuid4(),
        cache_key="artist-search:lost",
        entity_type="artist-search",
        entity_mbid="",
        response_data={"artists": []},
        cached_at=now,
        expires_at=now + timedelta(days=1),
    )


_SETTING = UserSetting(key="lost.key", value="1")

CALLS: dict[str, Callable[[Conn], object]] = {
    "user_settings.get": lambda c: PgUserSettingRepository(c).get("lost.key"),
    "user_settings.upsert": lambda c: PgUserSettingRepository(c).upsert(_SETTING),
    "user_settings.list_all": lambda c: PgUserSettingRepository(c).list_all(),
    "mb_cache.get": lambda c: PgMusicBrainzCacheRepository(c).get("artist-search:lost"),
    "mb_cache.get_many": lambda c: PgMusicBrainzCacheRepository(c).get_many(["k"]),
    "mb_cache.set": lambda c: PgMusicBrainzCacheRepository(c).set(_cache_row()),
    "mb_cache.set_many": lambda c: PgMusicBrainzCacheRepository(c).set_many([_cache_row()]),
    "mb_cache.delete_expired": lambda c: PgMusicBrainzCacheRepository(c).delete_expired(),
}


@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
def test_a_lost_connection_is_storage_unavailable(
    dead_conn: Conn, call: Callable[[Conn], object]
) -> None:
    with pytest.raises(StorageUnavailableError) as raised:
        call(dead_conn)
    assert isinstance(raised.value.__cause__, psycopg.OperationalError)


def test_a_lock_timeout_on_a_live_connection_is_not_translated(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as locker, connect_sync(migrated_db) as conn:
        locker.execute("LOCK TABLE user_settings IN ACCESS EXCLUSIVE MODE")
        conn.execute("SET lock_timeout = 200")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            PgUserSettingRepository(conn).get("lost.key")
        assert not conn.closed
        locker.rollback()
