"""Unit tests for the deferred content-hash backfill."""
from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from uuid import uuid4

from backend.domain.library import LibraryFile
from backend.services.hash_backfill_service import backfill_hash_batch
from tests.fakes.library_files import FakeLibraryFileRepository


def _indexed(path: Path) -> LibraryFile:
    """An unhashed row carrying the file's current stat, as phase 1 stores it."""
    st = path.stat()
    return LibraryFile(
        id=uuid4(), file_path=str(path), file_hash=None, format="flac",
        file_size=st.st_size, file_mtime_ns=st.st_mtime_ns,
    )


def _library(tmp_path: Path, n: int) -> tuple[FakeLibraryFileRepository, list[Path]]:
    repo = FakeLibraryFileRepository()
    paths = []
    for i in range(n):
        path = tmp_path / f"{i:02d}.flac"
        path.write_bytes(bytes([i]) * (100 + i))
        repo.upsert(_indexed(path))
        paths.append(path)
    return repo, paths


def _hash_of(repo: FakeLibraryFileRepository, path: Path) -> str | None:
    row = repo.get_by_path(str(path))
    assert row is not None
    return row.file_hash


def test_records_sha256_of_unchanged_files(tmp_path: Path) -> None:
    repo, paths = _library(tmp_path, 2)
    batch = backfill_hash_batch(repo, after_path=None, limit=10)
    assert (batch.hashed, batch.changed, batch.unreadable) == (2, 0, 0)
    for path in paths:
        assert _hash_of(repo, path) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_skips_file_changed_since_indexing(tmp_path: Path) -> None:
    repo, paths = _library(tmp_path, 1)
    paths[0].write_bytes(b"edited, and a different size")
    batch = backfill_hash_batch(repo, after_path=None, limit=10)
    assert (batch.hashed, batch.changed) == (0, 1)
    assert _hash_of(repo, paths[0]) is None


def test_skips_deleted_file(tmp_path: Path) -> None:
    repo, paths = _library(tmp_path, 1)
    paths[0].unlink()
    batch = backfill_hash_batch(repo, after_path=None, limit=10)
    assert (batch.hashed, batch.unreadable) == (0, 1)
    assert _hash_of(repo, paths[0]) is None


def test_skips_file_written_while_it_was_read(tmp_path: Path) -> None:
    repo, paths = _library(tmp_path, 1)

    def hash_then_append(path: Path) -> str:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with path.open("ab") as fh:
            fh.write(b"more")
        return digest

    batch = backfill_hash_batch(repo, after_path=None, limit=10, hash_file=hash_then_append)
    assert batch.changed == 1
    assert _hash_of(repo, paths[0]) is None


def test_does_not_overwrite_a_row_rescanned_meanwhile(tmp_path: Path) -> None:
    repo, paths = _library(tmp_path, 1)

    def rescan_then_hash(path: Path) -> str:
        repo.upsert(dataclasses.replace(_indexed(path), file_hash="from-rescan"))
        return "stale"

    batch = backfill_hash_batch(repo, after_path=None, limit=10, hash_file=rescan_then_hash)
    assert batch.changed == 1
    assert _hash_of(repo, paths[0]) == "from-rescan"


def test_ignores_hashed_and_missing_rows(tmp_path: Path) -> None:
    repo, paths = _library(tmp_path, 3)
    first = repo.get_by_path(str(paths[0]))
    assert first is not None
    repo.upsert(dataclasses.replace(first, file_hash="already"))
    repo.mark_missing(str(paths[1]))

    batch = backfill_hash_batch(repo, after_path=None, limit=10)

    assert batch.hashed == 1
    assert _hash_of(repo, paths[0]) == "already"
    assert _hash_of(repo, paths[1]) is None


def test_cursor_walks_rows_in_path_order_and_never_revisits(tmp_path: Path) -> None:
    repo, paths = _library(tmp_path, 5)
    paths[1].write_bytes(b"changed")  # stays unhashed; must not stall the cursor
    visited: list[str] = []
    cursor: str | None = None
    while True:
        batch = backfill_hash_batch(repo, after_path=cursor, limit=2)
        if batch.exhausted:
            break
        assert batch.last_path is not None
        visited.append(batch.last_path)
        cursor = batch.last_path

    assert visited == [str(paths[1]), str(paths[3]), str(paths[4])]
    assert repo.count_unhashed() == 1
