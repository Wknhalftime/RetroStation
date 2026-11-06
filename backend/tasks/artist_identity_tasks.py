"""The artist identity sweep (phase 2a S14; D6, C21, M33).

One sweep gives each MBID of a name holding several its artist, and the name its suggestion, by
reading ``library_files``, not by grouping: a file with a work is never grouped again, a fuzzy or
shortcut join never reaches ``upsert_local_artist``, and a tag arriving in a later scan must still
give the earlier MBID its artist. It runs last in a scan (``_run_scan``, and the watcher's
``library_scan_files_task``), and as a task the gate can run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import psycopg
import structlog
from psycopg import pq

from backend.config import get_settings
from backend.db.sync_conn import connect_sync
from backend.domain.system import StorageUnavailableError
from backend.services.repository_factory import RepositoryFactory
from backend.services.shared_name_service import apply_shared_name, plan_shared_names
from backend.tasks.huey_app import huey

logger = structlog.get_logger()

# A refused statement, or a dropped connection, which some adapters raise as their subdomain's
# StorageUnavailableError and others leave as psycopg's.
_DATABASE_FAILURES = (psycopg.Error, StorageUnavailableError)


@dataclass(frozen=True)
class ArtistIdentitySweep:
    """What one sweep did. ``names`` counts those holding several MBIDs, which is what it read."""

    names: int
    artists_made: int
    rows_written: int
    names_failed: int


def _commit_if_open(conn: psycopg.Connection[Any]) -> None:
    if conn.info.transaction_status == pq.TransactionStatus.INTRANS:
        conn.commit()


def _rollback_if_connected(conn: psycopg.Connection[Any]) -> None:
    if not conn.closed:
        conn.rollback()


def sweep_artist_identities(
    conn: psycopg.Connection[Any], repos: RepositoryFactory
) -> ArtistIdentitySweep | None:
    """Commit what is pending, read the library, then write each name in its own transaction. A
    failure on one name is logged and rolled back, and the next sweep retries it. None when the
    read itself failed. Either way it costs the scan nothing, a dropped connection included: the
    scan's work is committed by now, and its progress and hand-offs don't need this one."""
    try:
        _commit_if_open(conn)
        names = plan_shared_names(repos.artist_tag_files.list_tag_groups())
        _commit_if_open(conn)
    except _DATABASE_FAILURES:
        _rollback_if_connected(conn)
        logger.warning("artist_identity_sweep_read_failed", exc_info=True)
        return None
    artists_made = rows_written = names_failed = 0
    for shared in names:
        try:
            writes = apply_shared_name(shared, repos.artists, repos.library_suggestions)
            conn.commit()
        except _DATABASE_FAILURES as exc:
            _rollback_if_connected(conn)
            names_failed += 1
            logger.warning(
                "artist_identity_sweep_name_failed",
                normalized_name=shared.normalized_name,
                error=str(exc),
            )
            continue
        artists_made += writes.artists_made
        rows_written += writes.row_written
    sweep = ArtistIdentitySweep(len(names), artists_made, rows_written, names_failed)
    logger.info("artist_identity_sweep_complete", **asdict(sweep))
    return sweep


@huey.task()  # type: ignore[untyped-decorator]
def artist_identity_sweep_task() -> dict[str, int]:
    """One sweep on its own connection, with its counts; empty when its read failed."""
    with connect_sync(get_settings().database_url) as conn:
        sweep = sweep_artist_identities(conn, RepositoryFactory(conn))
    return asdict(sweep) if sweep is not None else {}
