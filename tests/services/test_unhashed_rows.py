"""Library-scan behaviour for rows whose content hash a first scan deferred."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from uuid import uuid4

from backend.domain.library import LibraryFile
from backend.services.library_scan_service import adopt_moved_row
from tests.fakes.library_files import FakeLibraryFileRepository


def _write(path: Path, data: bytes = b"\x00" * 100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _indexed_unhashed(path: Path, **overrides: object) -> LibraryFile:
    """A row as a tags-only first scan stores it: the file's stat, no hash."""
    st = path.stat()
    lf = LibraryFile(
        id=uuid4(),
        file_path=str(path),
        file_hash=None,
        format="flac",
        file_size=st.st_size,
        file_mtime_ns=st.st_mtime_ns,
    )
    return dataclasses.replace(lf, **overrides)  # type: ignore[arg-type]


class TestMoveDetectionWithoutHash:
    def test_file_without_hash_is_never_matched_by_hash(self, tmp_path: Path) -> None:
        repo = FakeLibraryFileRepository()
        gone = LibraryFile(
            id=uuid4(), file_path=str(tmp_path / "old" / "a.flac"), file_hash=None,
            format="flac", file_size=1, file_mtime_ns=1,
        )
        repo.upsert(gone)
        repo.mark_missing(gone.file_path)
        lf = _indexed_unhashed(_write(tmp_path / "new" / "a.flac"))

        assert adopt_moved_row(lf, repo) is None
        assert repo.get_by_path(gone.file_path) is not None
