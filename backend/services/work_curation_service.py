"""The Library's works writes: merge, split, reassign, format overrides, the master delete.

Moved from ``routers/library/works.py`` in phase 0c (C75), with today's behaviour, oddities
included: a merge drops a source's hand-picked master, and the master recalculation replaces
a manual pick that is no longer a present file of the work (phase 3b changes both). Each
write runs in the caller's transaction (a sync route's ``SyncRepos``) and commits nothing;
each refusal is a :class:`WorkCurationError` whose message is the route's detail.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import UUID, uuid4

from backend.domain.catalog import Recording, Work
from backend.domain.curation import (
    CurationFileMissingError,
    CurationFileNotFoundError,
    CurationFileNotOnWorkError,
    CurationWorkNotFoundError,
    FileAlreadyOnWorkError,
    FormatOverride,
    FormatOverrideExistsError,
    FormatOverrideNotFoundError,
    FormatOverrideNotStoredError,
    MasterCandidate,
    MergeResult,
    MergeTargetInSourcesError,
    NewFormatOverride,
    ReassignResult,
    SongMasterNotFoundError,
    SplitResult,
)
from backend.domain.enums import CatalogSource, FileStatus, SelectionMethod, VersionType
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.format_overrides import FormatOverrideRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.matches import MatchRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.work_curation import WorkCurationRepository
from backend.repositories.works import WorkRepository
from backend.services.master_selection_service import score_file_row

_NOTHING_MERGED = MergeResult(merged_file_count=0, deleted_work_count=0, dropped_override_count=0)


@dataclass(frozen=True)
class WorkCurationRepos:
    """The repositories the works writes run through, on one connection."""

    curation: WorkCurationRepository
    works: WorkRepository
    files: LibraryFileRepository
    recordings: RecordingRepository
    song_masters: SongMasterRepository
    overrides: FormatOverrideRepository
    matches: MatchRepository
    artists: ArtistCatalogRepository


def _work(work_id: str, works: WorkRepository) -> Work:
    work = works.get_by_id(work_id)
    if work is None:
        raise CurationWorkNotFoundError(f"Work {work_id} not found")
    return work


def _recording_of_version(
    work_id: str, version_type: VersionType, recordings: RecordingRepository
) -> str | None:
    """The work's recording of *version_type* (at most one: UNIQUE (work_id, version_type))."""
    return next(
        (r.id for r in recordings.get_by_work(work_id) if r.version_type == version_type), None
    )


# --- the master recalculation ------------------------------------------------------------------


def _score(candidate: MasterCandidate) -> tuple[int, int, int]:
    return score_file_row(asdict(candidate))


def _recalculate_master(work_id: str, repos: WorkCurationRepos) -> None:
    """Re-pick the work's automatic master from its present files.

    A manual master stays only while its file is a present file of the work; otherwise the
    best-scoring present file replaces it as the automatic pick, whatever the stored
    master's method. With no present file, a master on one of the work's own files stays
    (the file may come back) and one on a file of another work is removed.
    """
    master = repos.song_masters.get_by_work(work_id)
    if master is not None and master.selection_method == SelectionMethod.MANUAL:
        chosen = repos.files.get_by_id(master.preferred_file_id)
        if (
            chosen is not None
            and chosen.work_id == work_id
            and chosen.file_status == FileStatus.PRESENT
        ):
            return
    candidates = repos.curation.present_master_candidates(work_id)
    if not candidates:
        repos.curation.delete_master_off_work(work_id)
        return
    best = max(candidates, key=_score)
    master_id = master.id if master is not None else uuid4()
    repos.curation.store_auto_master(master_id, work_id, best.id, _score(best)[0])


def _settle_left_work(work_id: str, repos: WorkCurationRepos) -> bool:
    """Delete a work its last file just left, with its masters, overrides and recordings,
    else re-pick its master; whether it was deleted."""
    if repos.files.get_by_work(work_id):
        _recalculate_master(work_id, repos)
        return False
    repos.curation.release_work_matches(work_id)
    repos.song_masters.delete_by_work(work_id)
    repos.curation.delete_overrides_of_work(work_id)
    repos.curation.delete_recordings_of_work(work_id)
    repos.curation.delete_works([work_id])
    return True


# --- merge -------------------------------------------------------------------------------------


def _existing_works(work_ids: list[str], works: WorkRepository) -> list[Work]:
    found = (works.get_by_id(work_id) for work_id in dict.fromkeys(work_ids))
    return [work for work in found if work is not None]


def _consolidate_recordings(
    source_ids: list[str], target_id: str, repos: WorkCurationRepos
) -> None:
    """Move the sources' recordings to the target; one of a version the target already has
    hands its files to the target's and is deleted."""
    for recording in repos.curation.recordings_of_works(source_ids):
        kept = _recording_of_version(target_id, recording.version_type, repos.recordings)
        if kept is not None:
            repos.curation.move_recording_files(recording.id, kept)
            repos.curation.delete_recording(recording.id)
        else:
            repos.curation.move_recording(recording.id, target_id)


def _delete_unused_local_artists(artist_ids: set[str], repos: WorkCurationRepos) -> None:
    """Delete each local artist left with no work and no artist match."""
    for artist_id in artist_ids:
        artist = repos.artists.get_by_id(artist_id)
        if artist is None or artist.origin != CatalogSource.LOCAL:
            continue
        if repos.works.get_by_artist(artist_id):
            continue
        if not repos.curation.artist_has_match(artist_id):
            repos.curation.delete_artist(artist_id)


def merge_works(
    target_id: str, source_work_ids: list[str], repos: WorkCurationRepos
) -> MergeResult:
    """Fold the source works into the target: files, overrides, recordings, matches; the
    sources and their masters go, the target's master is re-picked."""
    target = _work(target_id, repos.works)
    if target_id in source_work_ids:
        raise MergeTargetInSourcesError("Target work ID must not appear in source_work_ids")
    sources = _existing_works(source_work_ids, repos.works) if source_work_ids else []
    if not sources:
        return _NOTHING_MERGED

    source_ids = [work.id for work in sources]
    merged = repos.curation.move_files_of_works(source_ids, target_id)
    dropped = repos.curation.drop_colliding_overrides(source_ids, target_id)
    repos.curation.move_overrides_of_works(source_ids, target_id)
    _consolidate_recordings(source_ids, target_id, repos)
    repos.curation.move_matches_of_works(source_ids, target_id)
    repos.curation.delete_masters_of_works(source_ids)
    deleted = repos.curation.delete_works(source_ids)
    _recalculate_master(target_id, repos)
    _delete_unused_local_artists({work.artist_id for work in sources} - {target.artist_id}, repos)
    return MergeResult(
        merged_file_count=merged, deleted_work_count=deleted, dropped_override_count=dropped
    )


# --- split ---------------------------------------------------------------------------------------


def split_file_off(work_id: str, file_id: UUID, repos: WorkCurationRepos) -> SplitResult:
    """Move a file onto a new work of its own (same title and artist), with a copy of its
    recording; the work it left is deleted if emptied, else its master is re-picked."""
    work = _work(work_id, repos.works)
    file = repos.files.get_by_id(file_id)
    if file is None or file.work_id != work_id:
        raise CurationFileNotOnWorkError(f"File {file_id} does not belong to work {work_id}")

    new_work_id = repos.works.create_local(work.title, work.artist_id)
    if file.recording_id is not None:
        old = repos.recordings.get_by_id(file.recording_id)
        copy = Recording(
            id=str(uuid4()),
            title=old.title if old is not None else work.title,
            work_id=new_work_id,
            version_type=old.version_type if old is not None else VersionType.ORIGINAL,
            duration_ms=old.duration_ms if old is not None else None,
            needs_enhancement=False,
        )
        repos.curation.insert_recording(copy)
        repos.curation.set_file_recording(file_id, copy.id)
    repos.files.update_work_id(file_id, new_work_id)
    repos.matches.move_to_work(file_id, new_work_id)
    repos.curation.store_split_master(uuid4(), new_work_id, file_id)
    left_deleted = _settle_left_work(work_id, repos)
    return SplitResult(new_work_id=new_work_id, old_work_deleted=left_deleted)


# --- reassign ------------------------------------------------------------------------------------


def _repoint_recording(
    file_id: UUID, recording_id: str, target_id: str, repos: WorkCurationRepos
) -> None:
    """Put the file on a recording of the target work.

    The target's recording of the same version comes first; else a recording shared with
    other files is copied onto the target (title, version, duration) and keeps them, and one
    the file holds alone moves itself.
    """
    shared = repos.curation.count_other_files_on_recording(recording_id, file_id) > 0
    old = repos.recordings.get_by_id(recording_id)
    version = old.version_type if old is not None else VersionType.ORIGINAL
    kept = _recording_of_version(target_id, version, repos.recordings)
    if kept is not None:
        repos.curation.set_file_recording(file_id, kept)
    elif shared:
        copy = Recording(
            id=str(uuid4()),
            title=old.title if old is not None else "",
            work_id=target_id,
            version_type=version,
            duration_ms=old.duration_ms if old is not None else None,
            needs_enhancement=False,
        )
        repos.curation.insert_recording(copy)
        repos.curation.set_file_recording(file_id, copy.id)
    else:
        repos.curation.move_recording(recording_id, target_id)


def reassign_file(file_id: UUID, work_id: str, repos: WorkCurationRepos) -> ReassignResult:
    """Move a file onto another work; its recording and matches follow, the target's master
    is re-picked, and the work it left is deleted if emptied, else its master is re-picked."""
    file = repos.files.get_by_id(file_id)
    if file is None:
        raise CurationFileNotFoundError(f"File {file_id} not found")
    _work(work_id, repos.works)
    if file.work_id == work_id:
        raise FileAlreadyOnWorkError(f"File {file_id} is already assigned to work {work_id}")

    if file.recording_id is not None:
        _repoint_recording(file_id, file.recording_id, work_id, repos)
    repos.files.update_work_id(file_id, work_id)
    repos.matches.move_to_work(file_id, work_id)
    _recalculate_master(work_id, repos)
    left_deleted = False
    if file.work_id is not None:
        left_deleted = _settle_left_work(file.work_id, repos)
    return ReassignResult(old_work_id=file.work_id, old_work_deleted=left_deleted)


# --- format overrides and the master delete --------------------------------------------------


def create_format_override(
    work_id: str, new: NewFormatOverride, repos: WorkCurationRepos
) -> FormatOverride:
    """Store a per-format preferred file for a work: a present file of the work, one per
    format."""
    _work(work_id, repos.works)
    file = repos.files.get_by_id(new.preferred_file_id)
    if file is None or file.work_id != work_id:
        raise CurationFileNotOnWorkError(
            f"File {new.preferred_file_id} does not belong to work {work_id}"
        )
    if file.file_status != FileStatus.PRESENT:
        raise CurationFileMissingError(f"File {new.preferred_file_id} is missing from disk")
    override = FormatOverride(
        id=uuid4(),
        work_id=work_id,
        format_name=new.format_name,
        preferred_file_id=new.preferred_file_id,
        notes=new.notes,
    )
    if not repos.curation.insert_override_if_absent(override):
        raise FormatOverrideExistsError(
            f"Format override for work {work_id} and format '{new.format_name}' already exists"
        )
    stored = repos.overrides.get(work_id, new.format_name)
    if stored is None:
        raise FormatOverrideNotStoredError(f"Format override {override.id} was not stored")
    return stored


def delete_format_override(
    work_id: str, override_id: UUID, overrides: FormatOverrideRepository
) -> None:
    """Delete one of the work's format overrides."""
    if all(o.id != override_id for o in overrides.list_by_work(work_id)):
        raise FormatOverrideNotFoundError(
            f"Format override {override_id} not found for work {work_id}"
        )
    overrides.delete(override_id)


def delete_master(work_id: str, song_masters: SongMasterRepository) -> None:
    """Delete the work's song master, whatever its method."""
    if song_masters.get_by_work(work_id) is None:
        raise SongMasterNotFoundError(f"No master found for work {work_id}")
    song_masters.delete_by_work(work_id)
