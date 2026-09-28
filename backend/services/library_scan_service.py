"""
Library scan service — directory walking and per-folder reconcile policy.

Public API:
  scan_directory(root, on_progress=None, on_file=None, on_quarantine=None)
                           -> (list[LibraryFile], list[LibraryQuarantine])
  scan_folder_incrementally(*, folder_path, file_repo, quarantine_repo)
                           -> FolderScanResult

Tag and file reading (read_tags, disk_stat, SUPPORTED_EXTENSIONS and the
mutagen extraction helpers) lives in backend.services.audio_tags — split
out under AUD-009 because this module's second reason to change is scan
and move-detection policy, not how a tag is parsed.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from uuid import UUID, uuid4

import structlog
from mutagen._util import MutagenError

from backend.domain.enums import FileStatus
from backend.domain.library import LibraryFile, LibraryQuarantine
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.library_quarantine import LibraryQuarantineRepository
from backend.services.audio_tags import SUPPORTED_EXTENSIONS, DiskStat, disk_stat, read_tags

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_directory(
    root: Path,
    on_progress: Callable[[int, int, str], None] | None = None,
    on_file: Callable[[LibraryFile], None] | None = None,
    on_quarantine: Callable[[LibraryQuarantine], None] | None = None,
) -> tuple[list[LibraryFile], list[LibraryQuarantine]]:
    """
    Walk *root* recursively and extract tags from all supported audio files.

    Returns ``(files, quarantine)`` where *quarantine* contains an entry for
    every file that raised a :exc:`mutagen.MutagenError`.

    Optional callbacks:
      *on_file* — called with each successfully extracted :class:`LibraryFile`.
      *on_quarantine* — called with each :class:`LibraryQuarantine` entry.
      *on_progress* — called with ``(processed, total, current_path)`` every
        50 files and on the final file.
    """
    candidates = sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    total = len(candidates)

    if total == 0:
        logger.warning("scan_directory_no_candidates", root=str(root))

    files: list[LibraryFile] = []
    quarantine: list[LibraryQuarantine] = []

    for processed_idx, path in enumerate(candidates, start=1):
        try:
            lf = read_tags(path)
            files.append(lf)
            if on_file is not None:
                on_file(lf)
        except MutagenError as exc:
            logger.warning("Quarantining %s: %s", path, exc)
            entry = LibraryQuarantine(
                id=uuid4(),
                file_path=str(path),
                error_message=str(exc),
            )
            quarantine.append(entry)
            if on_quarantine is not None:
                on_quarantine(entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error scanning %s: %s", path, exc)
            entry = LibraryQuarantine(
                id=uuid4(),
                file_path=str(path),
                error_message=f"{type(exc).__name__}: {exc}",
            )
            quarantine.append(entry)
            if on_quarantine is not None:
                on_quarantine(entry)

        if on_progress is not None and (processed_idx % 50 == 0 or processed_idx == total):
            on_progress(processed_idx, total, str(path))

    return files, quarantine


@dataclass
class FolderScanResult:
    """Result counts from an incremental per-folder scan."""

    files_written: int = 0
    files_skipped: int = 0
    files_missing: int = 0
    files_reappeared: int = 0
    quarantined: int = 0
    # New paths whose audio fingerprint or size and mtime matched a row whose
    # file had moved away; the row was repointed, keeping its links, instead
    # of a bare row inserted.
    files_relocated: int = 0
    # The folder exists but could not be listed (permissions, a share
    # dropping mid-scan). Nothing was diffed and nothing marked missing.
    folder_unreadable: bool = False
    # Quarantine entries dropped because their file read cleanly or is gone.
    quarantine_cleared: int = 0
    # Every path that failed this visit; their quarantine entries stand.
    failing_paths: set[str] = field(default_factory=set)

    def record_failure(self, file_path: str) -> None:
        self.quarantined += 1
        self.failing_paths.add(file_path)


def quarantine_once(
    quarantine_repo: LibraryQuarantineRepository,
    file_path_str: str,
    error: str,
) -> None:
    """Quarantine a file unless it already is; every scan that reaches it retries it."""
    if quarantine_repo.get_by_path(file_path_str) is not None:
        return
    quarantine_repo.create_write_only(
        LibraryQuarantine(id=uuid4(), file_path=file_path_str, error_message=error)
    )


def clear_resolved_quarantine(
    paths: Iterable[str],
    still_failing: set[str],
    quarantine_repo: LibraryQuarantineRepository,
) -> int:
    """Drop the quarantine entries in *paths* whose file did not fail this visit.

    Call only after a visit that reached every file those paths could name:
    a file that did not fail either read cleanly or is no longer on disk,
    and either way its entry is stale. Returns the number of paths cleared.
    """
    resolved = [p for p in paths if p not in still_failing]
    for path in resolved:
        quarantine_repo.delete_by_path(path)
    return len(resolved)


def _read_tags_safe(
    path: Path,
    file_path_str: str,
    quarantine_repo: LibraryQuarantineRepository,
    result: FolderScanResult,
) -> LibraryFile | None:
    """Read *path*'s tags; on any failure quarantine the file and return None."""
    try:
        return read_tags(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("scan_smart_quarantine", path=file_path_str, error=str(exc))
        quarantine_once(quarantine_repo, file_path_str, str(exc))
        result.record_failure(file_path_str)
        return None


def _mark_missing_files(
    existing_by_path: dict[str, LibraryFile],
    file_repo: LibraryFileRepository,
    result: FolderScanResult,
) -> None:
    """Mark files that are in DB but absent from disk as MISSING."""
    for file_path_str, existing in existing_by_path.items():
        if existing.file_status == FileStatus.PRESENT:
            file_repo.mark_missing(file_path_str)
            result.files_missing += 1


def _reread_and_upsert(
    path: Path,
    file_repo: LibraryFileRepository,
    quarantine_repo: LibraryQuarantineRepository,
    result: FolderScanResult,
) -> None:
    lf = _read_tags_safe(path, str(path), quarantine_repo, result)
    if lf is not None:
        file_repo.upsert(lf)
        result.files_written += 1


def _is_same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _is_gone_or_same_file(old: Path, new: Path) -> bool:
    """True when *old* no longer exists, or names *new* under another spelling.

    The second case is a case-only rename on a case-insensitive filesystem,
    where the old spelling still resolves to the renamed file.
    """
    return not os.path.exists(old) or _is_same_file(old, new)


def _spelled_as_on_disk(folder: Path) -> bool:
    """False when *folder* resolves only because the filesystem ignores case.

    After a case-only folder rename the old spelling still opens the folder,
    and a listing through it reports every file under the old spelling.
    realpath gives the disk's own spelling; when it differs by more than
    case (a junction, a subst drive, 8.3 short names) the spelling is
    accepted as it is.
    """
    real = os.path.realpath(folder)
    if os.path.normcase(real) != os.path.normcase(str(folder)):
        return True
    return real == str(folder)


def _move_candidates(lf: LibraryFile, file_repo: LibraryFileRepository) -> list[LibraryFile]:
    """Rows *lf* may have been moved from.

    Rows with the same audio fingerprint, which survives a retag, plus rows
    with the same size and mtime whatever their fingerprint state (a move or
    rename on one volume keeps both). Audio matches come first; a row found
    both ways is kept once.
    """
    candidates = file_repo.get_by_audio_hash(lf.audio_hash) if lf.audio_hash is not None else []
    if lf.file_size is not None and lf.file_mtime_ns is not None:
        candidates += file_repo.get_by_stat(lf.file_size, lf.file_mtime_ns)
    return _deduplicated_by_id(candidates)


def _deduplicated_by_id(rows: list[LibraryFile]) -> list[LibraryFile]:
    """*rows* with later duplicates (by id) dropped, keeping first-seen order."""
    seen_ids: set[UUID] = set()
    unique: list[LibraryFile] = []
    for row in rows:
        if row.id not in seen_ids:
            seen_ids.add(row.id)
            unique.append(row)
    return unique


def _respelled_from(lf: LibraryFile, file_repo: LibraryFileRepository) -> LibraryFile | None:
    """The row whose path is *lf*'s in another case and names the same file.

    That is a case-only rename, whatever the file now holds: taggers rename
    and retag in one go, so its content no longer matches the old row.
    """
    for candidate in file_repo.get_by_path_ignoring_case(lf.file_path):
        if candidate.file_path != lf.file_path and _is_same_file(
            Path(candidate.file_path),
            Path(lf.file_path),
        ):
            return candidate
    return None


def _moved_from(lf: LibraryFile, file_repo: LibraryFileRepository) -> LibraryFile | None:
    """The row this newly seen file was moved or renamed from, if any.

    A row with the same audio whose own file is still on disk is a
    duplicate copy, not the origin of a move, and is left alone.
    """
    respelled = _respelled_from(lf, file_repo)
    if respelled is not None:
        return respelled
    gone = [
        candidate
        for candidate in _move_candidates(lf, file_repo)
        if candidate.file_path != lf.file_path
        and _is_gone_or_same_file(Path(candidate.file_path), Path(lf.file_path))
    ]
    return _choose_move_origin(lf, gone)


def _file_name(path: str) -> str:
    return PureWindowsPath(path).name


def _track_position(lf: LibraryFile) -> tuple[str | None, int | None, int | None]:
    return (lf.audio.release_title, lf.audio.disc_number, lf.audio.track_number)


def _choose_move_origin(lf: LibraryFile, gone: list[LibraryFile]) -> LibraryFile | None:
    """The row among *gone* that *lf* was moved from, or None when that is not clear.

    The only candidate; else the only one on the same release, disc and
    track, when *lf* carries all three; else the only one with the same
    file name. Bit-identical twins moved together otherwise match each
    other's rows, and adopting the wrong one would swap their ids, so an
    undecided choice adopts nothing.
    """
    if len(gone) == 1:
        return gone[0]
    position = _track_position(lf)
    if None not in position:
        same_track = [c for c in gone if _track_position(c) == position]
        if len(same_track) == 1:
            return same_track[0]
    name = _file_name(lf.file_path)
    same_name = [c for c in gone if _file_name(c.file_path) == name]
    return same_name[0] if len(same_name) == 1 else None


def adopt_moved_row(lf: LibraryFile, file_repo: LibraryFileRepository) -> str | None:
    """Repoint the row *lf* was moved or renamed from at *lf*'s path.

    For a path the DB has no row for. Keeps the old row's id and every
    grouping/enrichment link that a bare insert would leave behind.
    Returns the old path, or None when *lf* is not a move.
    """
    origin = _moved_from(lf, file_repo)
    if origin is None:
        return None
    file_repo.relocate(origin.id, lf.file_path)
    return origin.file_path


def mark_unseen_missing(
    root: Path,
    seen_paths: set[str],
    file_repo: LibraryFileRepository,
) -> int:
    """Mark PRESENT files under *root* that a complete walk did not see as MISSING.

    A walk that saw nothing marks nothing: a root that is absent, or an
    empty mount-point folder, almost always means a drive that is not
    plugged in, not a library that was deleted. Returns the count marked.
    """
    if not seen_paths:
        logger.warning("mark_unseen_missing_skipped_empty_walk", root=str(root))
        return 0
    unseen = [
        path
        for path, status in file_repo.get_path_statuses_under(str(root)).items()
        if status == FileStatus.PRESENT and path not in seen_paths
    ]
    for path in unseen:
        file_repo.mark_missing(path)
    return len(unseen)


def _index_new_file(
    path: Path,
    file_repo: LibraryFileRepository,
    quarantine_repo: LibraryQuarantineRepository,
    result: FolderScanResult,
) -> str | None:
    """Scenario 3: index a path the DB has no row for.

    A moved or renamed file adopts its old row so grouping and enrichment
    links survive. Returns the old path it was moved from, else None.
    """
    lf = _read_tags_safe(path, str(path), quarantine_repo, result)
    if lf is None:
        return None
    moved_from = adopt_moved_row(lf, file_repo)
    if moved_from is not None:
        result.files_relocated += 1
    file_repo.upsert(lf)
    result.files_written += 1
    return moved_from


def _stat_matches(existing: LibraryFile, disk: DiskStat) -> bool | None:
    """Compare stored size+mtime with disk. None when the row predates stat tracking."""
    if existing.file_size is None or existing.file_mtime_ns is None:
        return None
    return existing.file_size == disk.size and existing.file_mtime_ns == disk.mtime_ns


@dataclass(frozen=True)
class FolderScanContext:
    """What one folder visit writes through, and the counts it keeps."""

    file_repo: LibraryFileRepository
    quarantine_repo: LibraryQuarantineRepository
    result: FolderScanResult


def _unchanged_on_disk(existing: LibraryFile, path: Path, ctx: FolderScanContext) -> bool | None:
    """Whether *path*'s size and mtime still equal the row's; None if it cannot be stat'ed."""
    try:
        disk = disk_stat(path)
    except OSError as exc:
        logger.warning("stat_failed", path=str(path), error=str(exc))
        ctx.result.record_failure(str(path))
        return None
    return _stat_matches(existing, disk) is True


def _restore_reappeared_file(existing: LibraryFile, path: Path, ctx: FolderScanContext) -> None:
    """Scenario 4: a MISSING row is back on disk.

    Unchanged size and mtime: restore it PRESENT unread, keeping its
    enrichment. Otherwise re-read its tags into the same row (same path, so
    the upsert keeps its id and links).
    """
    unchanged = _unchanged_on_disk(existing, path, ctx)
    if unchanged is None:
        return
    if unchanged:
        existing.file_status = FileStatus.PRESENT
        ctx.file_repo.upsert(existing)
    else:
        _reread_and_upsert(path, ctx.file_repo, ctx.quarantine_repo, ctx.result)
    ctx.result.files_reappeared += 1


def _reconcile_present_file(existing: LibraryFile, path: Path, ctx: FolderScanContext) -> None:
    """Scenarios 1 and 2 for a PRESENT row: size and mtime decide.

    - stored stat matches disk              -> unchanged, skip unread
    - stored stat differs, or none stored   -> re-read the tags
    """
    unchanged = _unchanged_on_disk(existing, path, ctx)
    if unchanged is None:
        return
    if unchanged:
        ctx.result.files_skipped += 1
        return
    _reread_and_upsert(path, ctx.file_repo, ctx.quarantine_repo, ctx.result)


def _visit_disk_file(
    path: Path,
    existing: LibraryFile | None,
    ctx: FolderScanContext,
) -> str | None:
    """Send one file found on disk to its scenario, by the row the DB holds for it.

    Returns the old path a new file was moved or renamed from, else None.
    """
    if existing is None:
        return _index_new_file(path, ctx.file_repo, ctx.quarantine_repo, ctx.result)
    if existing.file_status == FileStatus.MISSING:
        _restore_reappeared_file(existing, path, ctx)
    else:
        _reconcile_present_file(existing, path, ctx)
    return None


def _list_audio_files(folder_path: Path) -> dict[str, Path] | None:
    """Audio files directly in *folder_path*; empty if it is gone, None if unlistable.

    Gone and unreadable must stay distinct: gone marks every file missing,
    while a folder we merely failed to list still holds its files. A
    spelling the folder no longer has (a case-only rename) is gone: its
    files belong to the new spelling.
    """
    if not folder_path.is_dir() or not _spelled_as_on_disk(folder_path):
        return {}
    try:
        return {
            str(entry): entry
            for entry in folder_path.iterdir()
            if entry.is_file() and entry.suffix.lower() in SUPPORTED_EXTENSIONS
        }
    except OSError as exc:
        logger.warning("scan_folder_unreadable", path=str(folder_path), error=str(exc))
        return None


def scan_folder_incrementally(
    *,
    folder_path: Path,
    file_repo: LibraryFileRepository,
    quarantine_repo: LibraryQuarantineRepository,
) -> FolderScanResult:
    """Incremental scan of a single folder — diffs disk vs DB, handles all 6 scenarios.

    Only processes files directly in this folder (not recursive).

    Scenarios:
      1. Unchanged file (size + mtime match)          -> skip: no read, no DB write
      2. Modified file (size or mtime differ, or no   -> re-read tags, upsert
         stat stored yet)                                (enrichment resets)
      3. New file (not in DB)                         -> read tags, insert; if moved or
                                                         renamed, adopt the old row
      4. Re-appeared file (MISSING in DB)             -> restore PRESENT; re-read into the
                                                         same row if size or mtime differ
      5. Missing file (in DB, not on disk)            -> mark MISSING
      6. Parse failure (Mutagen error)                -> quarantine (once per path)

    Afterwards, quarantine entries for this folder's files that did not fail
    this visit are dropped: the file now reads, or is gone.
    """
    result = FolderScanResult()
    ctx = FolderScanContext(file_repo, quarantine_repo, result)

    existing_by_path: dict[str, LibraryFile] = {
        f.file_path: f for f in file_repo.get_by_folder_path(str(folder_path))
    }

    disk_files = _list_audio_files(folder_path)
    if disk_files is None:
        result.folder_unreadable = True
        return result

    for file_path_str, path in disk_files.items():
        moved_from = _visit_disk_file(path, existing_by_path.pop(file_path_str, None), ctx)
        if moved_from is not None:
            # A rename within this folder: the old spelling is no longer
            # a row to mark missing.
            existing_by_path.pop(moved_from, None)

    _mark_missing_files(existing_by_path, file_repo, result)

    # Non-recursive, like the rest of the visit: a child folder's entries
    # are judged when that folder is visited.
    in_folder = {
        p
        for p in quarantine_repo.get_paths_under(str(folder_path))
        if Path(p).parent == folder_path
    }
    result.quarantine_cleared = clear_resolved_quarantine(
        in_folder,
        result.failing_paths,
        quarantine_repo,
    )

    return result
