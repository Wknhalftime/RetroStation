"""Move one library file from its work to another, and settle both works."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog

from backend.domain.library import LibraryFile
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.matches import MatchRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.works import WorkRepository
from backend.services.master_selection_service import reselect_master_from_files

logger = structlog.get_logger()


@dataclass(frozen=True)
class WorkMoveRepos:
    """The repositories a move writes through."""

    files: LibraryFileRepository
    works: WorkRepository
    song_masters: SongMasterRepository
    matches: MatchRepository


def place_ungrouped_file(
    file_id: UUID,
    work_id: str,
    files: LibraryFileRepository,
    matches: MatchRepository,
) -> None:
    """Give a file with no work the work grouping chose; its matches take that work too.

    A match stores its file's work when it is made, so one made while the file
    had no work has none. The file left no work, so there is no master to settle.
    """
    files.update_work_id(file_id, work_id)
    matches.move_to_work(file_id, work_id)


def move_file_to_work(library_file: LibraryFile, work_id: str, repos: WorkMoveRepos) -> None:
    """Attach a file to another work; its matches follow it.

    The work the file joins gets (or re-picks) an auto song master from the
    files on it. The work the file left is deleted with its master once
    nothing references it; otherwise its master is re-picked from its own
    present files, so it never points at the moved file (see
    :func:`_settle_previous_work`).
    """
    previous_work_id = library_file.work_id
    if previous_work_id == work_id:
        return
    repos.files.update_work_id(library_file.id, work_id)
    repos.matches.move_to_work(library_file.id, work_id)
    reselect_master_from_files(work_id, repos.song_masters, repos.files)
    if previous_work_id is not None:
        _settle_previous_work(previous_work_id, library_file.id, work_id, repos)


def _settle_previous_work(
    previous_work_id: str,
    file_id: UUID,
    moved_to: str,
    repos: WorkMoveRepos,
) -> None:
    """Settle the work a file just left (see :func:`_settle_work`)."""
    if _settle_work(previous_work_id, repos):
        logger.info(
            "empty_work_deleted_after_move",
            work_id=previous_work_id,
            file_id=str(file_id),
            moved_to=moved_to,
        )


def _settle_work(work_id: str, repos: WorkMoveRepos) -> bool:
    """Delete a work nothing references, or else re-pick its master; whether it was deleted.

    A deleted work takes its now-stale song master with it. A work that stays
    re-picks its master from its own present files, so it never points at a
    file on another work: a manual pick on a file still in the work is kept,
    an invalid one becomes the automatic pick, and with no present file left
    the master is removed.
    """
    if repos.works.delete_if_empty(work_id):
        return True
    reselect_master_from_files(work_id, repos.song_masters, repos.files)
    return False
