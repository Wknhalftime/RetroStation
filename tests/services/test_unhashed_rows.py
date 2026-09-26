"""Library-scan behaviour for rows whose content hash a first scan deferred."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.library import LibraryFile
from backend.services.library_scan_service import adopt_moved_row, scan_folder_incrementally
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.library_quarantine import FakeLibraryQuarantineRepository


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


def _fresh_extract(path: Path, file_hash: str = "new_hash") -> LibraryFile:
    """What extract_tags returns for a freshly read file: hash, stat, no links."""
    st = path.stat()
    return LibraryFile(
        id=uuid4(), file_path=str(path), file_hash=file_hash, format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        file_size=st.st_size, file_mtime_ns=st.st_mtime_ns,
    )


_HASH = "backend.services.library_scan_service.compute_file_hash"
_EXTRACT = "backend.services.library_scan_service.extract_tags"


class TestReappearedUnhashed:
    def test_same_stat_is_restored_unread(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "album" / "a.flac")
        repo, q = FakeLibraryFileRepository(), FakeLibraryQuarantineRepository()
        repo.upsert(_indexed_unhashed(path, enrichment_status=EnrichmentStatus.ENRICHED))
        repo.mark_missing(str(path))

        with patch(_HASH, side_effect=AssertionError("hashed")), \
             patch(_EXTRACT, side_effect=AssertionError("re-read")):
            result = scan_folder_incrementally(
                folder_path=path.parent, file_repo=repo, quarantine_repo=q,
            )

        got = repo.get_by_path(str(path))
        assert got is not None
        assert (result.files_reappeared, result.files_written) == (1, 0)
        assert got.file_status == FileStatus.PRESENT
        assert got.enrichment_status == EnrichmentStatus.ENRICHED
        assert got.file_hash is None

    def test_changed_stat_is_reread(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "album" / "a.flac")
        repo, q = FakeLibraryFileRepository(), FakeLibraryQuarantineRepository()
        repo.upsert(_indexed_unhashed(
            path, enrichment_status=EnrichmentStatus.ENRICHED,
            file_size=path.stat().st_size + 1,
        ))
        repo.mark_missing(str(path))

        with patch(_EXTRACT, return_value=_fresh_extract(path)):
            result = scan_folder_incrementally(
                folder_path=path.parent, file_repo=repo, quarantine_repo=q,
            )

        got = repo.get_by_path(str(path))
        assert got is not None
        assert (result.files_reappeared, result.files_written) == (1, 1)
        assert got.file_hash == "new_hash"
        assert got.enrichment_status == EnrichmentStatus.PENDING


class TestPresentUnhashed:
    def test_row_without_stat_or_hash_is_reread(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "album" / "a.flac")
        repo, q = FakeLibraryFileRepository(), FakeLibraryQuarantineRepository()
        repo.upsert(_indexed_unhashed(path, file_size=None, file_mtime_ns=None))

        with patch(_EXTRACT, return_value=_fresh_extract(path)):
            result = scan_folder_incrementally(
                folder_path=path.parent, file_repo=repo, quarantine_repo=q,
            )

        got = repo.get_by_path(str(path))
        assert got is not None
        assert result.files_written == 1
        assert got.file_hash == "new_hash"

    def test_row_with_matching_stat_is_skipped_unread(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "album" / "a.flac")
        repo, q = FakeLibraryFileRepository(), FakeLibraryQuarantineRepository()
        repo.upsert(_indexed_unhashed(path))

        with patch(_HASH, side_effect=AssertionError("hashed")), \
             patch(_EXTRACT, side_effect=AssertionError("re-read")):
            result = scan_folder_incrementally(
                folder_path=path.parent, file_repo=repo, quarantine_repo=q,
            )

        assert (result.files_skipped, result.files_written) == (1, 0)


class TestMoveOfUnhashedFile:
    def test_new_file_adopts_missing_unhashed_row_with_same_stat(self, tmp_path: Path) -> None:
        old = tmp_path / "unsorted" / "kiss.flac"  # never on disk: already moved away
        new = _write(tmp_path / "Prince" / "kiss.flac")
        repo, q = FakeLibraryFileRepository(), FakeLibraryQuarantineRepository()
        repo.upsert(_indexed_unhashed(
            new, file_path=str(old), work_id="w-kiss",
            enrichment_status=EnrichmentStatus.ENRICHED,
        ))
        repo.mark_missing(str(old))

        with patch(_EXTRACT, return_value=_fresh_extract(new, "kiss_hash")):
            result = scan_folder_incrementally(
                folder_path=new.parent, file_repo=repo, quarantine_repo=q,
            )

        got = repo.get_by_path(str(new))
        assert got is not None
        assert result.files_relocated == 1
        assert repo.get_by_path(str(old)) is None
        assert got.work_id == "w-kiss"
        assert got.enrichment_status == EnrichmentStatus.ENRICHED
        assert got.file_hash == "kiss_hash"
        assert len(repo.get_by_hash("kiss_hash")) == 1

    def test_copy_does_not_adopt_unhashed_row_whose_file_still_exists(
        self, tmp_path: Path,
    ) -> None:
        old = _write(tmp_path / "unsorted" / "kiss.flac")
        new = _write(tmp_path / "Prince" / "kiss.flac")
        st = old.stat()
        os.utime(new, ns=(st.st_atime_ns, st.st_mtime_ns))
        repo, q = FakeLibraryFileRepository(), FakeLibraryQuarantineRepository()
        repo.upsert(_indexed_unhashed(old, work_id="w-kiss"))

        with patch(_EXTRACT, return_value=_fresh_extract(new, "kiss_hash")):
            result = scan_folder_incrementally(
                folder_path=new.parent, file_repo=repo, quarantine_repo=q,
            )

        assert result.files_relocated == 0
        assert repo.get_by_path(str(old)) is not None
        assert repo.get_by_path(str(new)) is not None
