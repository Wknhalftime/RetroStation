"""
Folder hash service — Merkle-tree-like change detection for library directories.

Uses mtime + file size (not content hashing) for fast change detection.
"""
from __future__ import annotations

import hashlib
import os
import unicodedata
from pathlib import Path
from uuid import UUID, uuid4

import structlog

from backend.domain.library import LibraryFolder
from backend.repositories.library_folders import LibraryFolderRepository
from backend.services.library_scan_service import SUPPORTED_EXTENSIONS

logger = structlog.get_logger()


def canonicalize_path(path: str) -> str:
    """Normalize a path for consistent DB storage and comparison."""
    normalized = os.path.normpath(path)
    return unicodedata.normalize("NFC", normalized)


def compute_folder_hash(
    folder_path: Path,
    child_hashes: list[str] | None = None,
) -> str:
    """Compute a hash for a folder based on mtime+size of audio files and child hashes.

    Args:
        folder_path: Path to the directory.
        child_hashes: Pre-computed hashes of child folders (sorted).

    Returns:
        SHA-256 hex digest representing the folder's current state.
    """
    file_parts: list[str] = []
    try:
        for entry in sorted(os.scandir(folder_path), key=lambda e: e.name):
            if not entry.is_file():
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            stat = entry.stat()
            file_parts.append(f"{entry.name}:{stat.st_mtime}:{stat.st_size}")
    except OSError as exc:
        logger.warning("compute_folder_hash_scandir_failed", path=str(folder_path), error=str(exc))

    parts = sorted(child_hashes or []) + sorted(file_parts)
    combined = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(combined).hexdigest()


def coalesce_paths(paths: list[str]) -> list[str]:
    """Merge child paths into parent paths where the parent is also in the list.

    If both /music/jazz and /music/jazz/miles are changed, keep only /music/jazz.
    """
    if not paths:
        return []

    sorted_paths = sorted(paths)
    result: list[str] = []

    for path in sorted_paths:
        normalized = path.rstrip("/").rstrip("\\")
        # Check if any already-added path is a parent of this one
        if any(
            normalized.startswith(r + "/") or normalized.startswith(r + "\\")
            for r in result
        ):
            continue
        result.append(normalized)

    return result


def _walk_folder_paths(root: Path) -> tuple[dict[str, str], list[str]]:
    """Walk root bottom-up; return (folder_hashes, all_dirs)."""
    folder_hashes: dict[str, str] = {}
    all_dirs: list[str] = []
    for dirpath, dirnames, _filenames in os.walk(str(root), topdown=False):
        canonical = canonicalize_path(dirpath)
        all_dirs.append(canonical)
        child_hashes = [
            folder_hashes[canonicalize_path(os.path.join(dirpath, d))]
            for d in sorted(dirnames)
            if canonicalize_path(os.path.join(dirpath, d)) in folder_hashes
        ]
        folder_hashes[canonical] = compute_folder_hash(Path(dirpath), child_hashes)
    return folder_hashes, all_dirs


def _sync_new_folders(
    all_dirs: list[str],
    folder_hashes: dict[str, str],
    existing_folders: dict[str, LibraryFolder],
    is_first_run: bool,
    folder_repo: LibraryFolderRepository,
) -> None:
    """Create DB rows for folders not yet in existing_folders (parents before children)."""
    for dir_path in reversed(all_dirs):
        if dir_path in existing_folders:
            continue
        parent_path = canonicalize_path(str(Path(dir_path).parent))
        parent = existing_folders.get(parent_path)
        folder = LibraryFolder(
            id=uuid4(),
            name=Path(dir_path).name,
            full_path=dir_path,
            parent_id=parent.id if parent else None,
            folder_hash=folder_hashes[dir_path] if is_first_run else None,
        )
        folder_repo.upsert(folder)
        existing_folders[dir_path] = folder


def _compute_diff(
    folder_hashes: dict[str, str],
    existing_folders: dict[str, LibraryFolder],
    in_flight_ids: set[UUID],
) -> tuple[list[str], list[tuple[UUID, str]]]:
    """Diff computed hashes against stored hashes; return (changed_paths, pending)."""
    changed: list[str] = []
    pending: list[tuple[UUID, str]] = []
    for dir_path, new_hash in folder_hashes.items():
        folder = existing_folders.get(dir_path)
        if folder is not None:
            if folder.id in in_flight_ids:
                continue
            pending.append((folder.id, new_hash))
            if folder.folder_hash != new_hash:
                changed.append(dir_path)
        else:
            changed.append(dir_path)
    return changed, pending


def diff_tree(
    root_path: str,
    folder_repo: LibraryFolderRepository,
    in_flight_ids: set[UUID] | None = None,
) -> tuple[list[str], list[tuple[UUID, str]]]:
    """Walk the directory tree and find folders whose hash has changed.

    Args:
        root_path: Root directory to walk.
        folder_repo: Repository for stored folder hashes.
        in_flight_ids: Set of folder IDs currently being processed (to skip them).

    Returns:
        A tuple of (changed_folder_paths, pending_hashes) where
        pending_hashes is a list of (folder_id, new_hash) ready for staging.
    """
    root = Path(canonicalize_path(root_path))
    if not root.is_dir():
        logger.warning("diff_tree_root_not_found", root=str(root))
        return [], []

    is_first_run = not folder_repo.has_any()
    folder_hashes, all_dirs = _walk_folder_paths(root)
    existing_folders: dict[str, LibraryFolder] = {
        f.full_path: f for f in folder_repo.get_all()
    }
    _sync_new_folders(all_dirs, folder_hashes, existing_folders, is_first_run, folder_repo)

    if is_first_run:
        for dir_path, new_hash in folder_hashes.items():
            folder_repo.update_hash(existing_folders[dir_path].id, new_hash)
        logger.info("diff_tree_first_run", folders=len(all_dirs))
        return [], []

    in_flight_ids = in_flight_ids or set()
    changed, pending = _compute_diff(folder_hashes, existing_folders, in_flight_ids)

    if in_flight_ids:
        logger.info("watcher_poll_skipped_in_flight", skipped=len(in_flight_ids))

    logger.info(
        "diff_tree_complete",
        total_folders=len(all_dirs),
        changed_folders=len(changed),
    )
    return changed, pending
