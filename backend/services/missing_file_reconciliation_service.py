"""Fold library rows whose file went missing into the present copy of the same track.

A file moved and retagged in one go (MusicBrainz Picard does both) changes
its content, so the scan cannot tell it was moved: it indexes the new path
as a new row and marks the old one missing, which still holds the file's
matches and song-master pick. For each missing row this finds the one
present row holding the same track, its successor, the way Navidrome pairs
missing tracks by persistent ID, with the audio fingerprint as the first,
exact rule: an audio match wins outright once every hashable candidate has
its fingerprint, and until then the pairing waits for a later run. Without
one, durations within 2 s and either the same recording and release MBIDs,
or the same artist, disc, track number and title on the same release, where
release titles are compared by their letters and digits only (Picard turns
"Radio (March 1999)" into "Radio, March 1999"). Rows with no successor, or
several equally good ones, are left alone.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from uuid import UUID

from backend.domain.enums import FileStatus, MatchStatus, ReasonCode
from backend.domain.library import (
    AUDIO_HASHABLE_FORMATS,
    LibraryFile,
    MissingFileCandidate,
    MissingFileChangedError,
    MissingFileDeletion,
    MissingFileEntry,
    MissingFileMove,
    MissingFileNotFoundError,
    MissingFilePage,
    MissingFilePlan,
    MissingFilePurge,
    MissingFileSelection,
    RemapTargetNotFoundError,
    RemapTargetNotPresentError,
    RemapTargetUngroupedError,
)
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.repositories.format_overrides import FormatOverrideRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.matches import MatchRepository
from backend.repositories.missing_files import MissingFileListingRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.works import WorkRepository
from backend.services.master_selection_service import reselect_master_from_files

# A remaster or an edit on the same release differs by more than this.
MAX_DURATION_DIFF_MS = 2_000


def _durations_agree(a: LibraryFile, b: LibraryFile) -> bool:
    da, db = a.audio.duration_ms, b.audio.duration_ms
    return da is not None and db is not None and abs(da - db) <= MAX_DURATION_DIFF_MS


def _same_recording(a: LibraryFile, b: LibraryFile) -> bool:
    return (
        a.audio.recording_mbid is not None
        and a.audio.recording_mbid == b.audio.recording_mbid
        and a.audio.release_mbid == b.audio.release_mbid
    )


def release_key(title: str | None) -> str | None:
    """*title* casefolded, with only its letters and digits; None when it has none.

    A retag that re-punctuates a release ("Radio (March 1999)" to "Radio,
    March 1999") keeps its key. Words are not rewritten: "&" is not "and".
    """
    if title is None:
        return None
    key = "".join(c for c in title.casefold() if c.isalnum())
    return key or None


def _release_track(f: LibraryFile) -> tuple[object, ...] | None:
    a = f.audio
    release = release_key(a.release_title)
    if (
        release is None
        or a.track_number is None
        or a.normalized_title is None
        or a.normalized_artist_name is None
    ):
        return None
    return (
        a.normalized_artist_name,
        release,
        a.disc_number,
        a.track_number,
        a.normalized_title,
    )


def _same_audio(a: LibraryFile, b: LibraryFile) -> bool:
    return a.audio_hash is not None and a.audio_hash == b.audio_hash


def is_same_track(missing: LibraryFile, candidate: LibraryFile) -> bool:
    """Whether *candidate* holds the track *missing* held.

    The same audio fingerprint, whatever the tags now say. Otherwise, with
    durations within 2 s: the same recording on the same release, or the
    same artist's disc, track number and title on a release whose title
    has the same letters and digits (release_key).
    """
    if _same_audio(missing, candidate):
        return True
    if not _durations_agree(missing, candidate):
        return False
    if _same_recording(missing, candidate):
        return True
    key = _release_track(missing)
    return key is not None and key == _release_track(candidate)


def _audio_twins(missing: LibraryFile, file_repo: LibraryFileRepository) -> list[LibraryFile]:
    if missing.audio_hash is None:
        return []
    return file_repo.get_by_audio_hash(missing.audio_hash)


def _recording_twins(missing: LibraryFile, file_repo: LibraryFileRepository) -> list[LibraryFile]:
    mbid = missing.audio.recording_mbid
    return file_repo.get_by_recording_mbid(mbid) if mbid is not None else []


def _release_track_twins(
    missing: LibraryFile,
    file_repo: LibraryFileRepository,
) -> list[LibraryFile]:
    a = missing.audio
    if (
        a.normalized_artist_name is None
        or release_key(a.release_title) is None
        or a.track_number is None
        or a.normalized_title is None
    ):
        return []
    return file_repo.get_present_by_track(
        a.normalized_artist_name, a.track_number, a.normalized_title
    )


def successor_candidates(
    missing: LibraryFile,
    file_repo: LibraryFileRepository,
) -> list[LibraryFile]:
    """Rows that may hold *missing*'s track; ready_successors decides."""
    found = {
        f.id: f
        for twins in (_audio_twins, _recording_twins, _release_track_twins)
        for f in twins(missing, file_repo)
    }
    return list(found.values())


def _file_name(path: str) -> str:
    return PureWindowsPath(path).name


def _pick(missing: LibraryFile, candidates: list[LibraryFile]) -> LibraryFile | None:
    """The only candidate, else the only one with *missing*'s file name, else None."""
    if len(candidates) == 1:
        return candidates[0]
    name = _file_name(missing.file_path)
    same_name = [c for c in candidates if _file_name(c.file_path) == name]
    return same_name[0] if len(same_name) == 1 else None


def ready_successors(missing: LibraryFile, candidates: list[LibraryFile]) -> list[LibraryFile]:
    """Candidates that can take over *missing* now: PRESENT, grouped, the same track."""
    return [
        c
        for c in candidates
        if c.id != missing.id
        and c.file_status == FileStatus.PRESENT
        and c.work_id is not None
        and is_same_track(missing, c)
    ]


def _awaiting_fingerprint(candidate: LibraryFile) -> bool:
    """Whether the hash backfill will still give *candidate* an audio fingerprint."""
    return candidate.audio_hash is None and candidate.format in AUDIO_HASHABLE_FORMATS


def choose_successor(missing: LibraryFile, ready: list[LibraryFile]) -> LibraryFile | None:
    """The successor among *ready*, or None to leave *missing* alone for now.

    An audio match wins outright once every hashable candidate has its
    fingerprint; until then None, as an unhashed candidate may be the true
    successor. Ties, and the other rules, settle by an identical file name.
    """
    audio = [c for c in ready if _same_audio(missing, c)]
    if audio and any(_awaiting_fingerprint(c) for c in ready):
        return None
    return _pick(missing, audio or ready)


def _move(missing: LibraryFile, successor: LibraryFile) -> MissingFileMove:
    return MissingFileMove(
        missing_id=missing.id,
        missing_path=missing.file_path,
        missing_work_id=missing.work_id,
        successor_id=successor.id,
        successor_path=successor.file_path,
        successor_work_id=successor.work_id,
    )


def plan_missing_file_moves(
    missing_rows: list[LibraryFile],
    candidates_for: Callable[[LibraryFile], list[LibraryFile]],
) -> MissingFilePlan:
    """Pair each missing row with its successor. Reads only.

    A successor can take over only one missing row per run: the first by
    path wins, and a later row whose pick is already claimed is ambiguous.
    """
    moves: list[MissingFileMove] = []
    ambiguous: list[str] = []
    unmatched: list[str] = []
    claimed: set[UUID] = set()
    for missing in sorted(missing_rows, key=lambda f: f.file_path):
        ready = ready_successors(missing, candidates_for(missing))
        successor = choose_successor(missing, ready) if ready else None
        if not ready:
            unmatched.append(missing.file_path)
        elif successor is None or successor.id in claimed:
            ambiguous.append(missing.file_path)
        else:
            claimed.add(successor.id)
            moves.append(_move(missing, successor))
    return MissingFilePlan(tuple(moves), tuple(ambiguous), tuple(unmatched))


@dataclass(frozen=True)
class ReconciliationRepos:
    """The repositories a fold writes through."""

    files: LibraryFileRepository
    matches: MatchRepository
    works: WorkRepository
    song_masters: SongMasterRepository
    format_overrides: FormatOverrideRepository


def apply_missing_file_move(move: MissingFileMove, repos: ReconciliationRepos) -> bool:
    """Move every reference to the missing row onto its successor, then delete it.

    When the successor sits in another work, the moved matches and format
    overrides take that work, and the work the missing row left re-picks its
    master from its own present files and is deleted once nothing references
    it. A move the library has overtaken since planning (the missing row came
    back, or the successor went missing) is skipped. Returns whether it folded.

    Both rows stay locked until the caller's transaction ends, so a concurrent
    scan cannot change either between the check and the writes.
    """
    if not repos.files.lock_fold_pair(move.missing_id, move.successor_id):
        return False
    repos.files.merge_into(move.missing_id, move.successor_id)
    if not move.crosses_work:
        return True
    repos.matches.move_to_work(move.successor_id, move.successor_work_id)
    if move.missing_work_id is not None and move.successor_work_id is not None:
        repos.format_overrides.move_to_work(
            move.successor_id, move.missing_work_id, move.successor_work_id
        )
    if move.missing_work_id is not None:
        reselect_master_from_files(move.missing_work_id, repos.song_masters, repos.files)
        repos.works.delete_if_empty(move.missing_work_id)
    return True


def repick_stranded_masters(repos: ReconciliationRepos) -> int:
    """Re-pick every AUTO master left on a missing file whose work has a present one.

    Manual masters are the user's and are left alone. Returns how many works
    were re-picked.
    """
    work_ids = repos.song_masters.list_work_ids_with_missing_master()
    for work_id in work_ids:
        reselect_master_from_files(work_id, repos.song_masters, repos.files)
    return len(work_ids)


def plan_for_library(file_repo: LibraryFileRepository) -> MissingFilePlan:
    """Plan reconciliation for every missing row in the library."""
    return plan_missing_file_moves(
        file_repo.get_missing(),
        lambda m: successor_candidates(m, file_repo),
    )


def _release_matches(
    file_id: UUID,
    matches: MatchRepository,
    identities: BroadcastTrackIdentityRepository,
) -> int:
    """Delete the file's matches; each identity left with none goes back to review."""
    identity_ids = matches.delete_for_file(file_id)
    for identity_id in set(identity_ids):
        if matches.get_by_identity(identity_id) is None:
            identities.update_match_status(
                identity_id, MatchStatus.NEEDS_REVIEW, None, ReasonCode.LIBRARY_FILE_REMOVED
            )
    return len(identity_ids)


def _detach_curation(file_id: UUID, repos: ReconciliationRepos) -> None:
    """Remove the file's overrides and masters; each work whose master it was re-picks one.

    Masters go before the re-pick: a Pg upsert never overwrites a MANUAL master,
    so re-picking first would leave a manual pick on the file being deleted.
    """
    repos.format_overrides.delete_for_file(file_id)
    for work_id in repos.song_masters.delete_for_file(file_id):
        reselect_master_from_files(work_id, repos.song_masters, repos.files)


def _delete_missing_row(
    file_id: UUID,
    repos: ReconciliationRepos,
    identities: BroadcastTrackIdentityRepository,
) -> int | None:
    """Delete one missing row; the matches it released, or None if it is not missing.

    Raises MissingFileChangedError when the row was restored after it was read.
    """
    row = repos.files.get_by_id(file_id)
    if row is None or row.file_status != FileStatus.MISSING:
        return None
    released = _release_matches(row.id, repos.matches, identities)
    _detach_curation(row.id, repos)
    if not repos.files.delete_missing(row.id):
        raise MissingFileChangedError(f"library file {row.id} is no longer missing")
    if row.work_id is not None:
        repos.works.delete_if_empty(row.work_id)
    return released


def _selected_ids(
    selection: MissingFileSelection,
    file_repo: LibraryFileRepository,
) -> tuple[UUID, ...]:
    """The ids *selection* names: its own ids, or every row missing now."""
    if selection.every_row:
        return tuple(f.id for f in file_repo.get_missing())
    return selection.ids


def delete_missing_files(
    selection: MissingFileSelection,
    repos: ReconciliationRepos,
    identities: BroadcastTrackIdentityRepository,
) -> MissingFileDeletion:
    """Delete the selected missing rows, releasing their matches back to review.

    Runs inside the caller's transaction and commits nothing. A row restored
    meanwhile (by a concurrent scan) raises MissingFileChangedError, and the
    caller rolls back the whole call.
    """
    deleted = released = skipped = 0
    for file_id in _selected_ids(selection, repos.files):
        outcome = _delete_missing_row(file_id, repos, identities)
        if outcome is None:
            skipped += 1
        else:
            deleted += 1
            released += outcome
    return MissingFileDeletion(deleted=deleted, matches_released=released, skipped=skipped)


def _remap_target(target_id: UUID, file_repo: LibraryFileRepository) -> LibraryFile:
    """The present, grouped file a missing row may be folded into; raises otherwise."""
    target = file_repo.get_by_id(target_id)
    if target is None:
        raise RemapTargetNotFoundError(f"File {target_id} not found")
    if target.file_status != FileStatus.PRESENT:
        raise RemapTargetNotPresentError(f"File {target_id} is missing from disk")
    if target.work_id is None:
        raise RemapTargetUngroupedError(f"File {target_id} has no work yet; scan the library")
    return target


def remap_missing_file(missing_id: UUID, target_id: UUID, repos: ReconciliationRepos) -> None:
    """Fold a missing row into the present file the user chose, as reconciliation does.

    Runs inside the caller's transaction and commits nothing. A fold a
    concurrent scan overtook raises the target's error if the target changed,
    else MissingFileNotFoundError; nothing was written either way.
    """
    missing = repos.files.get_by_id(missing_id)
    if missing is None or missing.file_status != FileStatus.MISSING:
        raise MissingFileNotFoundError(f"No missing file {missing_id}")
    move = _move(missing, _remap_target(target_id, repos.files))
    if not apply_missing_file_move(move, repos):
        _remap_target(target_id, repos.files)
        raise MissingFileNotFoundError(f"No missing file {missing_id}")


def _candidates(
    file_id: UUID,
    file_repo: LibraryFileRepository,
) -> tuple[MissingFileCandidate, ...]:
    """The present, grouped files holding the row's track, in path order.

    Every ready successor, not choose_successor's pick: the user settles ambiguous
    rows, and a row PR B is still waiting on may be remapped by hand.
    """
    missing = file_repo.get_by_id(file_id)
    if missing is None:
        return ()
    ready = ready_successors(missing, successor_candidates(missing, file_repo))
    return tuple(
        MissingFileCandidate(id=c.id, file_path=c.file_path)
        for c in sorted(ready, key=lambda c: c.file_path)
    )


def list_missing_files(
    offset: int,
    limit: int,
    listing: MissingFileListingRepository,
    file_repo: LibraryFileRepository,
) -> MissingFilePage:
    """A page of missing rows, each with the present files it could be folded into."""
    page = listing.list_page(offset, limit)
    entries = tuple(MissingFileEntry(row, _candidates(row.id, file_repo)) for row in page.rows)
    return MissingFilePage(entries, page.total, page.total_match_count)


def _has_present_copy(missing: LibraryFile, candidates: list[LibraryFile]) -> bool:
    """Whether a file on disk holds *missing*'s track, grouped or not.

    Wider than ready_successors (which also needs a work): a copy whose grouping
    failed this run still makes the row a fold, not a deletion.
    """
    return any(
        c.id != missing.id and c.file_status == FileStatus.PRESENT and is_same_track(missing, c)
        for c in candidates
    )


def unreplaced_rows(
    missing_rows: list[LibraryFile],
    candidates_for: Callable[[LibraryFile], list[LibraryFile]],
) -> list[LibraryFile]:
    """Missing rows no present file holds the track of.

    A row whose copy is present but not grouped yet waits for reconciliation, a
    row PR B is waiting on waits for the fingerprint, and an ambiguous row waits
    for the user; none of them is returned.
    """
    return [m for m in missing_rows if not _has_present_copy(m, candidates_for(m))]


def _awaits_fingerprint(missing: LibraryFile, fingerprints_pending: bool) -> bool:
    """Whether the hash backfill may still fold *missing* by its audio.

    A scan reads tags only, so a copy retagged and moved looks like nothing
    else until the backfill fingerprints it. A row with no fingerprint of its
    own can never be folded by audio.
    """
    return fingerprints_pending and missing.audio_hash is not None


@dataclass(frozen=True)
class DiskCheck:
    """What the disk says about missing rows' paths right now."""

    still_on_disk: frozenset[str]
    in_unreadable_folders: frozenset[str]


@dataclass(frozen=True)
class PurgeSelection:
    """The unreplaced rows a purge deletes, and how many it holds back and why."""

    ids: tuple[UUID, ...]
    awaiting_fingerprint: int
    still_on_disk: int
    unreadable_folder: int


def select_purge(
    unreplaced: list[LibraryFile],
    fingerprints_pending: bool,
    disk: DiskCheck,
) -> PurgeSelection:
    """Hold back rows the backfill may fold, and rows the walk may have missed. Pure.

    Each held-back row is counted once, under the first reason that holds it.
    """
    waiting = {m.id for m in unreplaced if _awaits_fingerprint(m, fingerprints_pending)}
    rest = [m for m in unreplaced if m.id not in waiting]
    on_disk = [m for m in rest if m.file_path in disk.still_on_disk]
    rest = [m for m in rest if m.file_path not in disk.still_on_disk]
    unreadable = [m for m in rest if m.file_path in disk.in_unreadable_folders]
    return PurgeSelection(
        ids=tuple(m.id for m in rest if m.file_path not in disk.in_unreadable_folders),
        awaiting_fingerprint=len(waiting),
        still_on_disk=len(on_disk),
        unreadable_folder=len(unreadable),
    )


def file_on_disk(file_path: str) -> bool:
    """Whether *file_path* is on disk, or the disk will not say it is gone.

    A file that exists is not missing, whatever the walk saw (an unreadable
    folder above it, or any other gap). A check the disk refuses keeps the row.
    """
    try:
        os.stat(file_path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


def folder_unreadable(folder: str) -> bool:
    """Whether *folder* is there but cannot be listed, so a walk skipped its files.

    A folder that no longer exists is not unreadable: its files really went.
    """
    try:
        with os.scandir(folder):
            return False
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True


def _in_unreadable_folders(rows: list[LibraryFile]) -> frozenset[str]:
    """Paths of *rows* whose folder is unreadable. Reads the disk, each folder once."""
    folder_of = {r.file_path: str(Path(r.file_path).parent) for r in rows}
    unreadable = {f for f in set(folder_of.values()) if folder_unreadable(f)}
    return frozenset(path for path, folder in folder_of.items() if folder in unreadable)


def _check_disk(rows: list[LibraryFile]) -> DiskCheck:
    """What the disk says about *rows* now. Reads the disk."""
    return DiskCheck(
        still_on_disk=frozenset(r.file_path for r in rows if file_on_disk(r.file_path)),
        in_unreadable_folders=_in_unreadable_folders(rows),
    )


def _missing_under(root: str, file_repo: LibraryFileRepository) -> list[LibraryFile]:
    """Every missing row beneath *root*."""
    under_root = {
        path
        for path, status in file_repo.get_path_statuses_under(root).items()
        if status == FileStatus.MISSING
    }
    return [m for m in file_repo.get_missing() if m.file_path in under_root]


def purge_unmatched_missing(
    root: str,
    repos: ReconciliationRepos,
    identities: BroadcastTrackIdentityRepository,
) -> MissingFilePurge:
    """Delete the missing rows under *root* that no present file replaces.

    Runs inside the caller's transaction and commits nothing. Holds back rows the
    hash backfill may still fold, rows whose file is on disk after all, and rows in
    a folder the walk could not list.
    """
    unreplaced = unreplaced_rows(
        _missing_under(root, repos.files), lambda m: successor_candidates(m, repos.files)
    )
    selection = select_purge(
        unreplaced, repos.files.count_audio_unhashed() > 0, _check_disk(unreplaced)
    )
    deletion = (
        delete_missing_files(MissingFileSelection(ids=selection.ids), repos, identities)
        if selection.ids
        else MissingFileDeletion(deleted=0, matches_released=0, skipped=0)
    )
    return MissingFilePurge(
        deleted=deletion.deleted,
        matches_released=deletion.matches_released,
        skipped=deletion.skipped,
        awaiting_fingerprint=selection.awaiting_fingerprint,
        still_on_disk=selection.still_on_disk,
        unreadable_folder=selection.unreadable_folder,
    )
