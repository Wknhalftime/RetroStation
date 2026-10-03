"""A stream read whose connection dies inside a repository is still a ``StreamReadError``.

Requirements: D88 (a stream read the database cannot answer is ``StreamReadError``, which
``/listen`` answers as 503) and the G1 final review: the user-settings repository now raises
``StorageUnavailableError`` for a lost connection, so ``bounded_connection`` must translate that
too, not only a raw ``OperationalError``. The stream service reads the listener limit through
this repository on every tune-in (``_station_and_limit``).
"""

from __future__ import annotations

import psycopg
import pytest

from backend.db.repositories.user_settings import PgUserSettingRepository
from backend.db.stream_reads import ReadBounds, bounded_connection
from backend.domain.streaming import StreamReadError

pytestmark = pytest.mark.integration

BOUNDS = ReadBounds(connect_timeout_s=5, lock_timeout_ms=300, statement_timeout_ms=1_500)


def test_a_settings_read_on_a_lost_connection_is_a_stream_read_error(migrated_db: str) -> None:
    with pytest.raises(StreamReadError), bounded_connection(migrated_db, BOUNDS) as conn:
        with psycopg.connect(migrated_db, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))
        PgUserSettingRepository(conn).get("stream.max_sessions")
