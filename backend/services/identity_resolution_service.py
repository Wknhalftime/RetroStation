"""Manual identity-resolution writes + post-commit master-selection recalc.

Splits the work into two functions with distinct failure semantics:

- ``persist_manual_match`` runs INSIDE the caller's async transaction. It
  derives ``work_id`` from the picked library file and inserts the match row.
  Anything raised here aborts the whole resolve — the txn rolls back, no
  partial state is left behind.

- ``recalculate_for_work_sync`` runs AFTER the caller commits, on a
  connection supplied by its ``repos_factory`` argument (mirrors
  ``identity_matching_task`` in ``backend/tasks/identity_matching_tasks.py``,
  which builds its own).
  This recalc is a best-effort side effect for *database* failures only:
  a ``psycopg.Error`` (a constraint violation, etc.) or a repository's
  ``StorageUnavailableError`` (the connection dropping) is caught and
  logged here so the durable match write is never
  undone by a recalc problem. Anything else — a bug in scoring, a bad
  work_id, whatever — is a genuine defect, not an expected operational
  failure, so it propagates instead of being silently absorbed. The router
  additionally wraps the to_thread call in its own try/except, which is
  the backstop for exactly that: non-DB errors from this function, plus
  thread/cancellation boundary errors this function's try can't reach.

This module builds no Pg adapters and never imports the database layer
(AUD-054): ``recalculate_for_work_sync`` depends only on the repository
ports in
``backend.repositories``, bundled into ``RecalcRepos``. The concrete wiring
— opening a sync connection and constructing the three Pg repositories —
lives in ``backend.services.repository_factory.recalc_repos``, which the
manual-resolve router passes in as ``repos_factory``. Every new collaborator
the recalc needs means editing ``RecalcRepos`` and ``recalc_repos``, not this
function's signature (same precedent as ``IdentityMatchingRepos``).

This file deliberately avoids both ``match_repo`` and the pre-existing raw
SQL in ``routers/matching.resolve_identity``: the manual-resolve endpoint
already does its other writes via raw SQL on the async connection; matching
that style here keeps a single, consistent failure surface for the manual
branch. Refactoring the whole endpoint onto repositories is a separate PR.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
import structlog
from psycopg import AsyncConnection

from backend.domain.enums import MatchTier, TargetType
from backend.domain.system import StorageUnavailableError
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.services import master_selection_service

logger = structlog.get_logger()


@dataclass(frozen=True)
class RecalcRepos:
    """Repository ports (+ commit) that ``recalculate_for_work_sync`` needs.

    Built by ``backend.services.repository_factory.recalc_repos`` from a
    db_url; the caller (the manual-resolve router) passes that function in
    as ``repos_factory`` so this module never imports the concrete adapters.
    """

    song_masters: SongMasterRepository
    recordings: RecordingRepository
    library_files: LibraryFileRepository
    commit: Callable[[], None]


RecalcReposFactory = Callable[[str], AbstractContextManager[RecalcRepos]]


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

    Returns the picked file's ``library_files.work_id`` (or ``None`` if it
    is unset) so the caller can dispatch a post-commit master-selection
    recalc. ``recording_id`` is intentionally NOT consulted as a fallback —
    ``matches.work_id`` is FK to ``works(id)`` and a recording_id (which
    lives in ``recordings(id)``) would fail the FK on insert. Migration
    0011 already backfills ``library_files.work_id`` from
    ``recordings.work_id`` at scan time, so a NULL here means the
    underlying work is genuinely unknown.

    The caller owns the surrounding ``UPDATE track_identities`` and
    ``DELETE FROM matches WHERE identity_id = %s`` writes plus the commit.
    """
    cur = await conn.execute(
        "SELECT work_id FROM library_files WHERE id = %s",
        (library_file_id,),
    )
    lf_row = await cur.fetchone()

    if lf_row is None:
        raise LibraryFileNotFoundError(f"library_file_id {library_file_id} does not exist")

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


def recalculate_for_work_sync(
    db_url: str,
    work_id: str,
    repos_factory: RecalcReposFactory,
) -> None:
    """Re-run song-master selection for a single work_id, post-commit.

    Best-effort, but only for the failure mode this actually expects: a
    ``psycopg.Error``, or the ``StorageUnavailableError`` a repository raises
    for a lost connection, is caught and logged here so the already-committed
    manual match is not undone by a transient DB problem. Anything else
    propagates — see the module docstring. ``repos_factory`` opens its own
    connection (the async endpoint connection has already been committed
    and released by the time this runs in a worker thread) — in production
    this is ``repository_factory.recalc_repos``.
    """
    try:
        with repos_factory(db_url) as repos:
            master_selection_service.recalculate_song_masters(
                work_ids=[work_id],
                song_master_repo=repos.song_masters,
                recording_repo=repos.recordings,
                library_file_repo=repos.library_files,
            )
            repos.commit()
    except (psycopg.Error, StorageUnavailableError):
        # Swallow-and-log: the durable write already committed in the
        # endpoint's async txn, and a DB hiccup here (dropped connection,
        # constraint violation, etc.) is exactly the transient/operational
        # failure this recalc is meant to tolerate. Master selection is
        # idempotent and will be retried whenever matching is re-run for
        # this work, so it is safe (and intentional) to absorb it here
        # rather than re-raise into the worker thread. Anything that is
        # NOT a database error is a genuine defect (bad scoring logic, a bad
        # work_id, ...) and is deliberately left to propagate to the
        # router's outer catch instead of being masked here.
        logger.warning(
            "manual_resolve_recalc_failed_inner",
            work_id=work_id,
            exc_info=True,
        )
