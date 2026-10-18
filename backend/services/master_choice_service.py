"""Make a file a work's master by hand (the crown on the work's page)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from backend.domain.curation import (
    MasterFileMissingError,
    MasterFileNotOnWorkError,
    MasterWorkNotFoundError,
    SongMaster,
)
from backend.domain.enums import FileStatus, SelectionMethod
from backend.domain.library import LibraryFile
from backend.repositories.recordings import RecordingRepository
from backend.services.work_move_service import WorkMoveRepos, move_file_to_work


@dataclass(frozen=True)
class MasterChoiceRepos:
    """The repositories a master choice reads and writes through."""

    move: WorkMoveRepos
    recordings: RecordingRepository


def choose_master(work_id: str, file_id: UUID, repos: MasterChoiceRepos) -> SongMaster:
    """Make a file the work's manual master; the master as stored.

    The work's page lists files by recording, so it also shows a file of another work that
    shares a recording with this one. Choosing such a file first moves it onto this work
    (see :func:`move_file_to_work`), keeping its recording; otherwise auto-selection would
    drop a master that points at another work's file.
    """
    if repos.move.works.get_by_id(work_id) is None:
        raise MasterWorkNotFoundError(f"Work {work_id} not found")
    library_file = repos.move.files.get_by_id(file_id)
    if library_file is None or not _shown_on_work(library_file, work_id, repos):
        raise MasterFileNotOnWorkError(f"File {file_id} does not belong to work {work_id}")
    if library_file.file_status != FileStatus.PRESENT:
        raise MasterFileMissingError(f"File {file_id} is missing from disk")

    move_file_to_work(library_file, work_id, repos.move)
    repos.move.song_masters.replace(
        SongMaster(
            id=uuid4(),
            work_id=work_id,
            preferred_file_id=file_id,
            selection_method=SelectionMethod.MANUAL,
        )
    )
    stored = repos.move.song_masters.get_by_work(work_id)
    if stored is None:
        raise RuntimeError(f"Expected a song master for work {work_id} after storing it")
    return stored


def _shown_on_work(library_file: LibraryFile, work_id: str, repos: MasterChoiceRepos) -> bool:
    """Whether the work's page lists the file: on the work, or on one of its recordings.

    The page's recordings are those the work owns plus those its own files are on.
    """
    if library_file.work_id == work_id:
        return True
    recording_id = library_file.recording_id
    if recording_id is None:
        return False
    recording = repos.recordings.get_by_id(recording_id)
    if recording is not None and recording.work_id == work_id:
        return True
    return any(f.work_id == work_id for f in repos.move.files.get_by_recording(recording_id))
