"""Unit tests for smart per-folder scan (all 6 scenarios)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.library import LibraryFile
from backend.services.library_scan_service import scan_folder_incrementally
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.library_quarantine import FakeLibraryQuarantineRepository


def _make_existing(
    *,
    file_path: str,
    file_hash: str = "existing_hash",
    enrichment_status: EnrichmentStatus = EnrichmentStatus.ENRICHED,
    file_status: FileStatus = FileStatus.PRESENT,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=file_path,
        file_hash=file_hash,
        format="flac",
        enrichment_status=enrichment_status,
        file_status=file_status,
    )


class TestScanFolderSmartUnchanged:
    """Scenario 1: File on disk, hash matches DB -> no DB write."""

    @patch("backend.services.library_scan_service._compute_file_hash", return_value="existing_hash")
    def test_unchanged_file_not_written(self, _mock: object, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        (folder / "track.flac").write_bytes(b"\x00" * 100)

        file_repo = FakeLibraryFileRepository()
        q_repo = FakeLibraryQuarantineRepository()
        existing = _make_existing(file_path=str(folder / "track.flac"))
        file_repo.upsert(existing)

        result = scan_folder_incrementally(folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo)

        assert result.files_written == 0
        assert result.files_skipped == 1
        f = file_repo.get_by_path(str(folder / "track.flac"))
        assert f.enrichment_status == EnrichmentStatus.ENRICHED


class TestScanFolderSmartModified:
    """Scenario 2: File on disk, hash differs -> update, reset enrichment."""

    @patch("backend.services.library_scan_service._compute_file_hash", return_value="new_hash")
    def test_modified_file_written(self, _mock: object, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        (folder / "track.flac").write_bytes(b"\x00" * 100)

        file_repo = FakeLibraryFileRepository()
        q_repo = FakeLibraryQuarantineRepository()
        existing = _make_existing(file_path=str(folder / "track.flac"), file_hash="old_hash")
        file_repo.upsert(existing)

        with patch("backend.services.library_scan_service.extract_tags") as mock_extract:
            mock_extract.return_value = LibraryFile(
                id=uuid4(),
                file_path=str(folder / "track.flac"),
                file_hash="new_hash",
                format="flac",
                enrichment_status=EnrichmentStatus.PENDING,
            )
            result = scan_folder_incrementally(folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo)

        assert result.files_written == 1


class TestScanFolderSmartNew:
    """Scenario 3: File on disk, no DB record -> insert as PENDING."""

    def test_new_file_inserted(self, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        (folder / "track.flac").write_bytes(b"\x00" * 100)

        file_repo = FakeLibraryFileRepository()
        q_repo = FakeLibraryQuarantineRepository()

        with patch("backend.services.library_scan_service.extract_tags") as mock_extract:
            mock_extract.return_value = LibraryFile(
                id=uuid4(),
                file_path=str(folder / "track.flac"),
                file_hash="brand_new",
                format="flac",
                enrichment_status=EnrichmentStatus.PENDING,
            )
            result = scan_folder_incrementally(folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo)

        assert result.files_written == 1
        f = file_repo.get_by_path(str(folder / "track.flac"))
        assert f is not None
        assert f.enrichment_status == EnrichmentStatus.PENDING


class TestScanFolderSmartReappeared:
    """Scenario 4: File on disk, MISSING in DB -> restore to PRESENT."""

    @patch("backend.services.library_scan_service._compute_file_hash", return_value="existing_hash")
    def test_reappeared_same_hash_preserves_enrichment(self, _mock: object, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        (folder / "track.flac").write_bytes(b"\x00" * 100)

        file_repo = FakeLibraryFileRepository()
        q_repo = FakeLibraryQuarantineRepository()
        existing = _make_existing(
            file_path=str(folder / "track.flac"),
            file_status=FileStatus.MISSING,
            enrichment_status=EnrichmentStatus.ENRICHED,
        )
        file_repo.upsert(existing)

        result = scan_folder_incrementally(folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo)

        assert result.files_reappeared == 1
        f = file_repo.get_by_path(str(folder / "track.flac"))
        assert f.file_status == FileStatus.PRESENT
        assert f.enrichment_status == EnrichmentStatus.ENRICHED


class TestScanFolderSmartMissing:
    """Scenario 5: File in DB but not on disk -> mark MISSING."""

    def test_missing_file_marked(self, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()

        file_repo = FakeLibraryFileRepository()
        q_repo = FakeLibraryQuarantineRepository()
        ghost = _make_existing(file_path=str(folder / "ghost.flac"), file_status=FileStatus.PRESENT)
        file_repo.upsert(ghost)

        result = scan_folder_incrementally(folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo)

        assert result.files_missing == 1
        f = file_repo.get_by_path(str(folder / "ghost.flac"))
        assert f.file_status == FileStatus.MISSING
        assert f.enrichment_status == EnrichmentStatus.ENRICHED


class TestScanFolderSmartParseFailure:
    """Scenario 6: File present but Mutagen fails -> quarantine."""

    def test_parse_failure_quarantined(self, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        (folder / "corrupt.flac").write_bytes(b"\x00" * 10)

        file_repo = FakeLibraryFileRepository()
        q_repo = FakeLibraryQuarantineRepository()

        from mutagen._util import MutagenError
        with patch("backend.services.library_scan_service.extract_tags", side_effect=MutagenError("bad file")):
            result = scan_folder_incrementally(folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo)

        assert result.quarantined == 1
        q = q_repo.get_by_path(str(folder / "corrupt.flac"))
        assert q is not None
