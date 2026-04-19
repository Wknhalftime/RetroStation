"""Transaction scaffolding for CSV ingestion.

Encapsulates "open a sync connection, acquire the per-station advisory
lock, yield repositories, commit" so the task layer doesn't mix settings
lookup, raw SQL, and repo wiring in one place.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from backend.config import get_settings
from backend.db.sync_conn import connect_sync
from backend.services.repository_factory import RepositoryFactory

# Postgres advisory locks take a signed bigint. We map each station to a
# stable 63-bit int so two uploads for the same station serialize on the
# same lock, regardless of UUID case or hyphenation.
_NO_STATION_LOCK_KEY = -1
_BIGINT_SIGNED_MAX = (1 << 63) - 1


def station_advisory_lock_key(station_id: str | None) -> int:
    """Map a station identifier to a stable signed-bigint lock key.

    UUID inputs are canonicalised via ``UUID(...).int`` so "A1B2..." and
    "a1b2..." collapse to the same key. Non-UUID strings fall back to a
    deterministic hash. An empty/missing station id gets a reserved key
    so station-less uploads still serialise among themselves.
    """
    if not station_id:
        return _NO_STATION_LOCK_KEY
    try:
        raw = UUID(station_id).int
    except ValueError:
        # Deterministic, but not UUID — hash the string bytes.
        raw = int.from_bytes(station_id.encode("utf-8"), "big", signed=False)
    # Fold to 63 bits (signed bigint range) without losing determinism.
    return raw % _BIGINT_SIGNED_MAX


@contextmanager
def ingest_transaction(station_id: str | None) -> Iterator[RepositoryFactory]:
    """Open a sync connection, take the ingest lock, yield repos, commit.

    The advisory lock is transaction-scoped — Postgres releases it on
    commit or rollback, so callers never need to release it manually.
    """
    settings = get_settings()
    with connect_sync(settings.database_url) as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (station_advisory_lock_key(station_id),),
        )
        yield RepositoryFactory(conn)
        conn.commit()
