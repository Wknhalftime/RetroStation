"""Unit tests for the audio-hash backfill."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from backend.domain.enums import AudioHashKind
from backend.domain.library import AudioHash, LibraryFile
from backend.services.audio_hash import compute_audio_hash
from backend.services.hash_backfill_service import backfill_hash_batch
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fixtures.audio_builders import tag_mp3, write_flac

OTHER = AudioHash(AudioHashKind.AUDIO_SHA256, "e" * 64)


def _indexed(path: Path) -> LibraryFile:
    """A row as a scan stores it: the file's stat, no fingerprint yet."""
    st = path.stat()
    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        format=path.suffix.lstrip("."),
        file_size=st.st_size,
        file_mtime_ns=st.st_mtime_ns,
    )


def _library(tmp_path: Path, n: int, *, store_md5: bool = False):
    repo = FakeLibraryFileRepository()
    paths = []
    for i in range(n):
        path = write_flac(tmp_path / f"{i:02d}.flac", [(i, -i)], store_md5=store_md5)
        repo.upsert(_indexed(path))
        paths.append(path)
    return repo, paths


def _hash_of(repo: FakeLibraryFileRepository, path: Path) -> AudioHash | None:
    row = repo.get_by_path(str(path))
    assert row is not None
    return row.audio_hash


def test_records_the_audio_hash_of_unchanged_files(tmp_path: Path) -> None:
    repo, paths = _library(tmp_path, 2)
    batch = backfill_hash_batch(repo, after_path=None, limit=10)
    assert (batch.hashed, batch.changed, batch.unreadable) == (2, 0, 0)
    for path in paths:
        assert _hash_of(repo, path) == compute_audio_hash(path)


def test_a_flac_with_a_stored_md5_is_fingerprinted_from_its_header(tmp_path: Path) -> None:
    repo, paths = _library(tmp_path, 1, store_md5=True)
    with patch(
        "backend.services.audio_hash._sha256_of_range",
        side_effect=AssertionError("read the audio bytes"),
    ):
        batch = backfill_hash_batch(repo, after_path=None, limit=10)
    got = _hash_of(repo, paths[0])
    assert batch.hashed == 1
    assert got is not None and got.kind == AudioHashKind.FLAC_MD5


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


def test_a_file_with_no_audio_is_left_unhashed(tmp_path: Path) -> None:
    path = tmp_path / "only-tags.mp3"
    path.write_bytes(b"")
    tag_mp3(path, "Nothing")
    repo = FakeLibraryFileRepository()
    repo.upsert(_indexed(path))

    batch = backfill_hash_batch(repo, after_path=None, limit=10)

    assert (batch.hashed, batch.unreadable) == (0, 1)
    assert _hash_of(repo, path) is None


def test_skips_file_written_while_it_was_read(tmp_path: Path) -> None:
    repo, paths = _library(tmp_path, 1)

    def hash_then_append(path: Path) -> AudioHash | None:
        digest = compute_audio_hash(path)
        with path.open("ab") as fh:
            fh.write(b"more")
        return digest

    batch = backfill_hash_batch(repo, after_path=None, limit=10, hash_audio=hash_then_append)
    assert batch.changed == 1
    assert _hash_of(repo, paths[0]) is None


def test_does_not_overwrite_a_row_rescanned_meanwhile(tmp_path: Path) -> None:
    repo, paths = _library(tmp_path, 1)

    def rescan_then_hash(path: Path) -> AudioHash | None:
        repo.upsert(dataclasses.replace(_indexed(path), audio_hash=OTHER))
        return compute_audio_hash(path)

    batch = backfill_hash_batch(repo, after_path=None, limit=10, hash_audio=rescan_then_hash)
    assert batch.changed == 1
    assert _hash_of(repo, paths[0]) == OTHER


def test_ignores_hashed_missing_and_unfingerprintable_rows(tmp_path: Path) -> None:
    repo, paths = _library(tmp_path, 3)
    first = repo.get_by_path(str(paths[0]))
    assert first is not None
    repo.upsert(dataclasses.replace(first, audio_hash=OTHER))
    repo.mark_missing(str(paths[1]))
    ogg = tmp_path / "x.ogg"
    ogg.write_bytes(b"OggS" + b"\x00" * 60)
    repo.upsert(_indexed(ogg))

    batch = backfill_hash_batch(repo, after_path=None, limit=10)

    assert batch.hashed == 1
    assert _hash_of(repo, paths[0]) == OTHER
    assert _hash_of(repo, paths[1]) is None
    assert _hash_of(repo, ogg) is None


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
    assert repo.count_audio_unhashed() == 1
