"""A curator's decision on one song, and the master recalculation after a manual match.

Resolve (a manual match), Reject and Unmatch write in the caller's transaction, on the
request's one sync connection (``SongDecisionRepos``, phase 3a PR 1, C75): the song's row
through ``SongDecisionRepository``, its matches through ``SongMatchService`` (C52). Anything
raised aborts the whole decision; the route's ``SyncRepos`` rolls it back.

``recalculate_for_work_sync`` runs AFTER the caller commits, on a connection supplied by its
``repos_factory`` argument (mirrors ``identity_matching_task`` in
``backend/tasks/identity_matching_tasks.py``, which builds its own). This recalc is a
best-effort side effect for *database* failures only: a ``psycopg.Error`` (a constraint
violation, etc.) or a repository's ``StorageUnavailableError`` (the connection dropping) is
caught and logged here so the durable match write is never undone by a recalc problem.
Anything else is a genuine defect, not an expected operational failure, so it propagates
instead of being silently absorbed. The router additionally wraps the call in its own
try/except, the backstop for exactly that.

This module builds no Pg adapters and never imports the database layer (AUD-054): it depends
only on the repository ports in ``backend.repositories``. The concrete wiring lives in
``backend.services.repository_factory`` (``recalc_repos``, ``song_decision_repos``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from uuid import UUID

import psycopg
import structlog

from backend.domain.enums import MatchStatus
from backend.domain.system import StorageUnavailableError
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.song_decisions import SongDecisionRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.services import master_selection_service
from backend.services.song_match_service import SongMatchService

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


class IdentityNotFoundError(IdentityResolutionError):
    """No broadcast song has this id."""


class SuggestionRequiredError(IdentityResolutionError):
    """A song in review can only be rejected together with the suggestion that was shown."""


class SongNotRejectableError(IdentityResolutionError):
    """The song's status does not allow this action."""


_MATCHED = (MatchStatus.AUTO_MATCHED.value, MatchStatus.MANUAL_MATCHED.value)
_IN_REVIEW = (MatchStatus.PENDING.value, MatchStatus.NEEDS_REVIEW.value)


@dataclass(frozen=True)
class SongDecisionRepos:
    """The ports a song's Resolve, Reject and Unmatch write through, and the request's commit,
    all on the request's one sync connection (built by ``repository_factory.
    song_decision_repos``; phase 3a PR 1, C75)."""

    songs: SongDecisionRepository
    files: LibraryFileRepository
    song_matches: SongMatchService
    commit: Callable[[], None]
    """Commit now: a lost connection is the domain's ``StorageUnavailableError``."""


def _lock_song_status(repos: SongDecisionRepos, identity_id: UUID) -> str:
    status = repos.songs.lock_status(identity_id)
    if status is None:
        raise IdentityNotFoundError(f"Identity {identity_id} not found")
    return status


def record_song_rejection(
    repos: SongDecisionRepos, identity_id: UUID, library_file_ids: Sequence[UUID]
) -> None:
    """Record rejected files and park the song, inside the caller's transaction.

    The one write behind both Reject and Unmatch (AUD-R022, D3/D5). The song becomes
    manual_rejected / USER_UNMATCHED and leaves review until the next matching run re-checks
    it without these files (AUD-R029, spec 2026-10-13 R2, R6). Its caller locked the row
    first, so a worker's uncommitted match insert cannot survive the delete that follows.
    """
    repos.songs.park_rejected(identity_id, library_file_ids)
    repos.song_matches.clear_song(identity_id)


def reject_song_suggestion(
    repos: SongDecisionRepos, identity_id: UUID, shown_file_id: UUID | None
) -> None:
    """Reject: a song in review rejects the suggestion shown; a matched song its matched file(s)."""
    status = _lock_song_status(repos, identity_id)
    if status in _MATCHED:
        files = repos.songs.matched_file_ids(identity_id)
    elif status in _IN_REVIEW:
        if shown_file_id is None:
            raise SuggestionRequiredError("library_file_id (the suggestion shown) is required")
        files = [shown_file_id]
    else:
        raise SongNotRejectableError(
            f"Identity {identity_id} is in {status!r}; it has no suggestion to reject"
        )
    record_song_rejection(repos, identity_id, files)


def unmatch_song(repos: SongDecisionRepos, identity_id: UUID) -> None:
    """Unmatch: a matched song is parked and its file(s) recorded as rejected (D5, R6)."""
    status = _lock_song_status(repos, identity_id)
    if status not in _MATCHED:
        raise SongNotRejectableError(
            f"Identity {identity_id} is in {status!r}; only matched songs can be unmatched"
        )
    record_song_rejection(repos, identity_id, repos.songs.matched_file_ids(identity_id))


def match_song_by_hand(
    repos: SongDecisionRepos, identity_id: UUID, library_file_id: UUID
) -> str | None:
    """Resolve: the song is manual_matched, with the manual tier, to the picked file.

    Its row is written first, so a worker's uncommitted match is waited for and then
    replaced. Returns the work to recalculate the master of (see ``persist_manual_match``).
    """
    if not repos.songs.exists(identity_id):
        raise IdentityNotFoundError(f"Identity {identity_id} not found")
    repos.songs.set_matched_by_hand(identity_id)
    return persist_manual_match(repos, identity_id, library_file_id)


def persist_manual_match(
    repos: SongDecisionRepos, identity_id: UUID, library_file_id: UUID
) -> str | None:
    """Replace the song's matches with the manual match, inside the caller's transaction.

    Returns the picked file's ``library_files.work_id`` (or ``None`` if it is unset) so the
    caller can dispatch a post-commit master-selection recalc. ``recording_id`` is
    intentionally NOT consulted as a fallback: ``matches.work_id`` is FK to ``works(id)``
    and a recording_id (which lives in ``recordings(id)``) would fail the FK on insert.
    Migration 0011 already backfills ``library_files.work_id`` from ``recordings.work_id``
    at scan time, so a NULL here means the underlying work is genuinely unknown.
    """
    picked = repos.files.get_by_id(library_file_id)
    if picked is None:
        raise LibraryFileNotFoundError(f"library_file_id {library_file_id} does not exist")
    derived_work_id: str | None = picked.work_id or None
    repos.song_matches.match_by_hand(identity_id, library_file_id, derived_work_id)
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
    connection (the route has already committed its own connection's
    transaction by the time this runs) — in production this is
    ``repository_factory.recalc_repos``.
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
        # route's own transaction, and a DB hiccup here (dropped connection,
        # constraint violation, etc.) is exactly the transient/operational
        # failure this recalc is meant to tolerate. Master selection is
        # idempotent and will be retried whenever matching is re-run for
        # this work, so it is safe (and intentional) to absorb it here
        # rather than re-raise into the route. Anything that is
        # NOT a database error is a genuine defect (bad scoring logic, a bad
        # work_id, ...) and is deliberately left to propagate to the
        # router's outer catch instead of being masked here.
        logger.warning(
            "manual_resolve_recalc_failed_inner",
            work_id=work_id,
            exc_info=True,
        )
