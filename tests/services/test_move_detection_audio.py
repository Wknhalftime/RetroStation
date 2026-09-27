"""Move detection: a moved and retagged file keeps its row through its audio fingerprint."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from backend.domain.enums import AudioHashKind, FileStatus
from backend.domain.library import AudioHash, LibraryFile
from backend.services.library_scan_service import adopt_moved_row  # ⚠ AUD-009
from tests.fakes.library_files import FakeLibraryFileRepository

H1 = AudioHash(AudioHashKind.FLAC_MD5, "1" * 32)
H2 = AudioHash(AudioHashKind.FLAC_MD5, "2" * 32)


def _write(path: Path, data: bytes = b"\x00" * 100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _row(
    path: Path,
    *,
    audio_hash: AudioHash | None,
    stat: tuple[int, int] = (1, 1),
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        file_hash=None,
        format="flac",
        work_id="w-kiss",
        file_size=stat[0],
        file_mtime_ns=stat[1],
        audio_hash=audio_hash,
    )


def _seen(path: Path, audio_hash: AudioHash | None) -> LibraryFile:
    """A newly seen file as read_tags returns it: stat and fingerprint, no links."""
    st = path.stat()
    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        file_hash=None,
        format="flac",
        file_size=st.st_size,
        file_mtime_ns=st.st_mtime_ns,
        audio_hash=audio_hash,
    )


def test_a_moved_and_retagged_file_adopts_the_row_with_its_audio_hash(tmp_path: Path) -> None:
    old = tmp_path / "unsorted" / "kiss.flac"  # never on disk: already moved away
    new = _write(tmp_path / "Prince" / "Parade" / "03 Kiss.flac", b"\x01" * 300)
    repo = FakeLibraryFileRepository()
    stored = repo.upsert(_row(old, audio_hash=H1))
    repo.mark_missing(str(old))

    assert adopt_moved_row(_seen(new, H1), repo) == str(old)

    moved = repo.get_by_id(stored.id)
    assert moved is not None
    assert (moved.file_path, moved.file_status, moved.work_id) == (
        str(new),
        FileStatus.PRESENT,
        "w-kiss",
    )


def test_a_row_not_yet_marked_missing_is_adopted_once_its_file_is_gone(tmp_path: Path) -> None:
    """The watcher may visit the new folder before the old one."""
    old = tmp_path / "unsorted" / "kiss.flac"
    new = _write(tmp_path / "Prince" / "kiss.flac", b"\x01" * 300)
    repo = FakeLibraryFileRepository()
    stored = repo.upsert(_row(old, audio_hash=H1))

    assert adopt_moved_row(_seen(new, H1), repo) == str(old)
    assert repo.get_by_id(stored.id) is not None


def test_audio_twin_whose_file_still_exists_is_not_adopted(tmp_path: Path) -> None:
    """An album track and its compilation copy share audio: both stay."""
    album = _write(tmp_path / "Parade" / "kiss.flac")
    best_of = _write(tmp_path / "Best Of" / "kiss.flac", b"\x01" * 300)
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(album, audio_hash=H1))

    assert adopt_moved_row(_seen(best_of, H1), repo) is None
    assert repo.get_by_path(str(album)) is not None


def test_a_different_audio_hash_is_not_a_move(tmp_path: Path) -> None:
    old = tmp_path / "unsorted" / "kiss.flac"
    new = _write(tmp_path / "Prince" / "kiss.flac")
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(old, audio_hash=H1))
    repo.mark_missing(str(old))

    assert adopt_moved_row(_seen(new, H2), repo) is None


def test_pending_row_with_the_same_stat_is_adopted(tmp_path: Path) -> None:
    """MP3s and FLACs without a stored MD5 have no fingerprint until the backfill."""
    old = tmp_path / "unsorted" / "kiss.mp3"
    new = _write(tmp_path / "Prince" / "kiss.mp3")
    st = new.stat()
    repo = FakeLibraryFileRepository()
    stored = repo.upsert(_row(old, audio_hash=None, stat=(st.st_size, st.st_mtime_ns)))
    repo.mark_missing(str(old))

    assert adopt_moved_row(_seen(new, None), repo) == str(old)
    assert repo.get_by_id(stored.id) is not None


def test_pending_row_is_not_adopted_by_a_copy_while_its_file_exists(tmp_path: Path) -> None:
    old = _write(tmp_path / "unsorted" / "kiss.mp3")
    new = _write(tmp_path / "Prince" / "kiss.mp3")
    st = old.stat()
    os.utime(new, ns=(st.st_atime_ns, st.st_mtime_ns))
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(old, audio_hash=None, stat=(st.st_size, st.st_mtime_ns)))

    assert adopt_moved_row(_seen(new, None), repo) is None
