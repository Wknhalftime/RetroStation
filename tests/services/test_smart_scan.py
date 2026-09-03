"""Unit tests for smart per-folder scan (all 6 scenarios + stat shortcut)."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.library import LibraryFile
from backend.services.library_scan_service import scan_folder_incrementally
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.library_quarantine import FakeLibraryQuarantineRepository

# A row indexed well before any test file is written, so the file's mtime is
# always newer than indexed_at and the legacy path cannot short-circuit.
_LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)


def _make_existing(
    *,
    file_path: str,
    file_hash: str = "existing_hash",
    enrichment_status: EnrichmentStatus = EnrichmentStatus.ENRICHED,
    file_status: FileStatus = FileStatus.PRESENT,
    file_size: int | None = None,
    file_mtime_ns: int | None = None,
    indexed_at: datetime | None = None,
) -> LibraryFile:
    lf = LibraryFile(
        id=uuid4(),
        file_path=file_path,
        file_hash=file_hash,
        format="flac",
        enrichment_status=enrichment_status,
        file_status=file_status,
        file_size=file_size,
        file_mtime_ns=file_mtime_ns,
    )
    if indexed_at is not None:
        lf.indexed_at = indexed_at
    return lf


def _stat_of(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def _fresh_extract(path: Path, file_hash: str = "new_hash") -> LibraryFile:
    """What extract_tags would return for a freshly read file: no links."""
    size, mtime_ns = _stat_of(path)
    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        file_hash=file_hash,
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        file_size=size,
        file_mtime_ns=mtime_ns,
    )


class TestScanFolderSmartUnchanged:
    """Scenario 1: legacy row (no stat), file newer than index, hash matches
    -> skip, no rewrite, but the stat is recorded so the next scan never
    reads the file again."""

    @patch("backend.services.library_scan_service._compute_file_hash", return_value="existing_hash")
    def test_unchanged_file_not_written(self, _mock: object, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        (folder / "track.flac").write_bytes(b"\x00" * 100)

        file_repo = FakeLibraryFileRepository()
        q_repo = FakeLibraryQuarantineRepository()
        existing = _make_existing(
            file_path=str(folder / "track.flac"), indexed_at=_LONG_AGO,
        )
        file_repo.upsert(existing)

        result = scan_folder_incrementally(
            folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo,
        )

        assert result.files_written == 0
        assert result.files_skipped == 1
        assert result.files_stat_backfilled == 1
        f = file_repo.get_by_path(str(folder / "track.flac"))
        assert f is not None
        assert f.enrichment_status == EnrichmentStatus.ENRICHED
        assert (f.file_size, f.file_mtime_ns) == _stat_of(folder / "track.flac")


class TestScanFolderSmartModified:
    """Scenario 2: File on disk, hash differs -> update, reset enrichment."""

    @patch("backend.services.library_scan_service._compute_file_hash", return_value="new_hash")
    def test_modified_file_written(self, _mock: object, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        (folder / "track.flac").write_bytes(b"\x00" * 100)

        file_repo = FakeLibraryFileRepository()
        q_repo = FakeLibraryQuarantineRepository()
        existing = _make_existing(
            file_path=str(folder / "track.flac"), file_hash="old_hash", indexed_at=_LONG_AGO,
        )
        file_repo.upsert(existing)

        with patch("backend.services.library_scan_service.extract_tags") as mock_extract:
            mock_extract.return_value = LibraryFile(
                id=uuid4(),
                file_path=str(folder / "track.flac"),
                file_hash="new_hash",
                format="flac",
                enrichment_status=EnrichmentStatus.PENDING,
            )
            result = scan_folder_incrementally(
            folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo,
        )

        assert result.files_written == 1


class TestScanFolderStatShortcut:
    """Size + mtime are checked before any content hash.

    The hash means reading every byte of the file; on a multi-hundred-GB
    library that is the difference between a scan that takes seconds and
    one that takes hours. The hash is the fallback, not the first check.
    """

    @patch(
        "backend.services.library_scan_service._compute_file_hash",
        side_effect=AssertionError("must not hash a file whose stat is unchanged"),
    )
    def test_matching_stat_skips_without_hashing(self, _mock: object, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        track = folder / "track.flac"
        track.write_bytes(b"\x00" * 100)
        size, mtime_ns = _stat_of(track)

        file_repo = FakeLibraryFileRepository()
        q_repo = FakeLibraryQuarantineRepository()
        file_repo.upsert(
            _make_existing(file_path=str(track), file_size=size, file_mtime_ns=mtime_ns),
        )

        result = scan_folder_incrementally(
            folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo,
        )

        assert result.files_skipped == 1
        assert result.files_written == 0
        assert result.files_stat_backfilled == 0

    def test_changed_size_triggers_reextract(self, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        track = folder / "track.flac"
        track.write_bytes(b"\x00" * 100)
        size, mtime_ns = _stat_of(track)

        file_repo = FakeLibraryFileRepository()
        q_repo = FakeLibraryQuarantineRepository()
        file_repo.upsert(
            _make_existing(file_path=str(track), file_size=size + 1, file_mtime_ns=mtime_ns),
        )

        with patch("backend.services.library_scan_service.extract_tags") as mock_extract:
            mock_extract.return_value = _fresh_extract(track)
            result = scan_folder_incrementally(
                folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo,
            )

        assert result.files_written == 1
        mock_extract.assert_called_once()

    def test_changed_mtime_triggers_reextract(self, tmp_path: Path) -> None:
        folder = tmp_path / "jazz"
        folder.mkdir()
        track = folder / "track.flac"
        track.write_bytes(b"\x00" * 100)
        size, mtime_ns = _stat_of(track)

        file_repo = FakeLibraryFileRepository()
        q_repo = FakeLibraryQuarantineRepository()
        file_repo.upsert(
            _make_existing(
                file_path=str(track), file_size=size, file_mtime_ns=mtime_ns - 1_000_000_000,
            ),
        )

        with patch("backend.services.library_scan_service.extract_tags") as mock_extract:
            mock_extract.return_value = _fresh_extract(track)
            result = scan_folder_incrementally(
                folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo,
            )

        assert result.files_written == 1

    @patch(
        "backend.services.library_scan_service._compute_file_hash",
        side_effect=AssertionError("must not hash a file older than its index row"),
    )
    def test_legacy_row_older_file_is_backfilled_not_hashed(
        self, _mock: object, tmp_path: Path,
    ) -> None:
        """Rows indexed before size/mtime were stored have neither.

        If the file's mtime predates indexed_at it cannot have changed since
        we last read it, so record the stat and move on without reading it.
        """
        folder = tmp_path / "jazz"
        folder.mkdir()
        track = folder / "track.flac"
        track.write_bytes(b"\x00" * 100)
        a_day_ago_ns = int((datetime.now(UTC) - timedelta(days=1)).timestamp() * 1e9)
        os.utime(track, ns=(a_day_ago_ns, a_day_ago_ns))

        file_repo = FakeLibraryFileRepository()
        q_repo = FakeLibraryQuarantineRepository()
        file_repo.upsert(_make_existing(file_path=str(track)))  # indexed_at = now

        result = scan_folder_incrementally(
            folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo,
        )

        assert result.files_skipped == 1
        assert result.files_stat_backfilled == 1
        f = file_repo.get_by_path(str(track))
        assert f is not None
        assert f.enrichment_status == EnrichmentStatus.ENRICHED
        assert (f.file_size, f.file_mtime_ns) == _stat_of(track)

    def test_legacy_row_newer_file_is_reextracted(self, tmp_path: Path) -> None:
        """A retag after indexing (mtime > indexed_at) must be picked up."""
        folder = tmp_path / "jazz"
        folder.mkdir()
        track = folder / "track.flac"
        track.write_bytes(b"\x00" * 100)

        file_repo = FakeLibraryFileRepository()
        q_repo = FakeLibraryQuarantineRepository()
        file_repo.upsert(
            _make_existing(file_path=str(track), file_hash="old_hash", indexed_at=_LONG_AGO),
        )

        with (
            patch(
                "backend.services.library_scan_service._compute_file_hash",
                return_value="new_hash",
            ),
            patch("backend.services.library_scan_service.extract_tags") as mock_extract,
        ):
            mock_extract.return_value = _fresh_extract(track)
            result = scan_folder_incrementally(
                folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo,
            )

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
            result = scan_folder_incrementally(
            folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo,
        )

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

        result = scan_folder_incrementally(
            folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo,
        )

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

        result = scan_folder_incrementally(
            folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo,
        )

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
        with patch(
            "backend.services.library_scan_service.extract_tags",
            side_effect=MutagenError("bad file"),
        ):
            result = scan_folder_incrementally(
                folder_path=folder, file_repo=file_repo, quarantine_repo=q_repo,
            )

        assert result.quarantined == 1
        q = q_repo.get_by_path(str(folder / "corrupt.flac"))
        assert q is not None
