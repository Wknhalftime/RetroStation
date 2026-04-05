"""
Folder hash service — Merkle-tree-like change detection for library directories.

Uses mtime + file size (not content hashing) for fast change detection.
"""
from __future__ import annotations

import hashlib
import os
import unicodedata
from pathlib import Path
from uuid import uuid4

import structlog

from backend.domain.models import LibraryFolder
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
    except OSError:
        pass

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


def diff_tree(
    root_path: str,
    folder_repo: LibraryFolderRepository,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Walk the directory tree and find folders whose hash has changed.

    Args:
        root_path: Root directory to walk.
        folder_repo: Repository for stored folder hashes.

    Returns:
        A tuple of (changed_folder_paths, pending_hashes) where
        pending_hashes is a list of (full_path, new_hash) for all walked folders.
    """
    root = Path(canonicalize_path(root_path))
    if not root.is_dir():
        logger.warning("diff_tree_root_not_found", root=str(root))
        return [], []

    is_first_run = not folder_repo.has_any()

    # Build folder structure bottom-up using os.walk
    folder_hashes: dict[str, str] = {}
    all_dirs: list[str] = []

    for dirpath, dirnames, _filenames in os.walk(str(root), topdown=False):
        canonical = canonicalize_path(dirpath)
        all_dirs.append(canonical)

        child_hashes = []
        for d in sorted(dirnames):
            child_path = canonicalize_path(os.path.join(dirpath, d))
            if child_path in folder_hashes:
                child_hashes.append(folder_hashes[child_path])

        folder_hash = compute_folder_hash(Path(dirpath), child_hashes)
        folder_hashes[canonical] = folder_hash

    # Build path -> existing folder mapping
    existing_folders: dict[str, LibraryFolder] = {
        f.full_path: f for f in folder_repo.get_all()
    }

    # Create missing folder entries
    for dir_path in all_dirs:
        if dir_path not in existing_folders:
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

    # First run: set all hashes and return no changes
    if is_first_run:
        for dir_path, new_hash in folder_hashes.items():
            folder = existing_folders[dir_path]
            folder_repo.update_hash(folder.id, new_hash)
        logger.info("diff_tree_first_run", folders=len(all_dirs))
        return [], []

    # Diff: find folders where hash changed
    changed: list[str] = []
    pending: list[tuple[str, str]] = []

    for dir_path, new_hash in folder_hashes.items():
        folder = existing_folders.get(dir_path)
        pending.append((dir_path, new_hash))
        if folder is None or folder.folder_hash != new_hash:
            changed.append(dir_path)

    logger.info(
        "diff_tree_complete",
        total_folders=len(all_dirs),
        changed_folders=len(changed),
    )
    return changed, pending
