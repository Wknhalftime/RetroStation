"""Manual identity-resolution writes + post-commit master-selection recalc.

Splits the work into two functions with distinct failure semantics:

- ``persist_manual_match`` runs INSIDE the caller's async transaction. It
  derives ``work_id`` from the picked library file and inserts the match row.
  Anything raised here aborts the whole resolve — the txn rolls back, no
  partial state is left behind.

- ``recalculate_for_work_sync`` runs AFTER the caller commits, on its own
  sync connection (mirrors ``backend/tasks/identity_matching_tasks.py:41``).
  This recalc is a best-effort side effect: failures are caught and logged
  so the durable match write is never undone by a recalc problem. The
  router additionally wraps the to_thread call in its own try/except — that
  outer catch covers thread/cancellation boundary errors that the
  in-function catch cannot reach.

This file deliberately avoids both ``match_repo`` and the pre-existing raw
SQL in ``routers/matching.resolve_identity``: the manual-resolve endpoint
already does its other writes via raw SQL on the async connection; matching
that style here keeps a single, consistent failure surface for the manual
branch. Refactoring the whole endpoint onto repositories is a separate PR.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import structlog
from psycopg import AsyncConnection

from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.song_masters import PgSongMasterRepository
from backend.db.sync_conn import connect_sync
from backend.domain.enums import MatchTier, TargetType
from backend.services import master_selection_service

logger = structlog.get_logger()


class IdentityResolutionError(Exception):
    """Base for failures specific to the manual identity-resolution flow."""


class LibraryFileNotFoundError(IdentityResolutionError):
    """Caller-supplied library_file_id does not exist.

    Raised by ``persist_manual_match`` so the endpoint can map it to a
    deterministic 422 instead of waiting for the FK violation to surface
    as a 500.
    """


async def persist_manual_match(
    conn: AsyncConnection[Any],
    identity_id: UUID,
    library_file_id: UUID,
) -> str | None:
    """Insert the manual match row inside the caller's transaction.

    Returns the derived ``work_id`` (or ``None`` if the picked file has
    neither a work_id nor a recording_id linkage) so the caller can
    dispatch a post-commit master-selection recalc.

    The caller owns the surrounding ``UPDATE track_identities`` and
    ``DELETE FROM matches WHERE identity_id = %s`` writes plus the commit.
    """
    cur = await conn.execute(
        "SELECT work_id FROM library_files WHERE id = %s",
        (library_file_id,),
    )
    lf_row = await cur.fetchone()

    if lf_row is None:
        raise LibraryFileNotFoundError(
            f"library_file_id {library_file_id} does not exist"
        )

    # Only the file's real work_id — never recording_id as a stand-in.
    # matches.work_id is FK to works(id); a recording_id would fail the
    # FK on insert. Migration 0011 backfills library_files.work_id from
    # recordings.work_id at scan time, so any recording-with-a-work has
    # lib_file.work_id populated; a NULL here means the work is genuinely
    # unknown and the post-commit recalc is correctly skipped.
    derived_work_id: str | None = lf_row["work_id"] or None

    await conn.execute(
        """INSERT INTO matches
               (id, identity_id, library_file_id, target_type, work_id,
                confidence_score, match_tier)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            uuid4(),
            identity_id,
            library_file_id,
            TargetType.LIBRARY_FILE.value,
            derived_work_id,
            1.0,
            MatchTier.MANUAL.value,
        ),
    )

    return derived_work_id


def recalculate_for_work_sync(db_url: str, work_id: str) -> None:
    """Re-run song-master selection for a single work_id, post-commit.

    Best-effort: any failure is caught and logged here so the
    already-committed manual match is not undone. Opens its own sync
    connection (the async endpoint connection has already been committed
    and released by the time this runs in a worker thread).
    """
    try:
        with connect_sync(db_url) as conn:
            master_selection_service.recalculate_song_masters(
                work_ids=[work_id],
                song_master_repo=PgSongMasterRepository(conn),
                recording_repo=PgRecordingRepository(conn),
                library_file_repo=PgLibraryFileRepository(conn),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        # Swallow-and-log: the durable write already committed in the
        # endpoint's async txn. Master selection is idempotent and will
        # be retried whenever matching is re-run for this work, so it is
        # safe (and intentional) to absorb every failure mode here rather
        # than re-raise into the worker thread.
        logger.warning(
            "manual_resolve_recalc_failed_inner",
            work_id=work_id,
            exc_info=True,
        )
