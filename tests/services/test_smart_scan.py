"""The per-folder scan: its six scenarios, decided by size + mtime, reading tags only."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from mutagen._util import MutagenError

from backend.domain.enums import EnrichmentStatus, FileStatus
from backend.domain.library import LibraryFile
from backend.services.library_scan_service import (  # ⚠ AUD-009
    FolderScanResult,
    scan_folder_incrementally,
)
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.library_quarantine import FakeLibraryQuarantineRepository

# ⚠ AUD-009: patch the names where scan_folder_incrementally looks them up.
_READ = "backend.services.library_scan_service.read_tags"
_STAT = "backend.services.library_scan_service.disk_stat"
_WHOLE_FILE = "backend.services.library_scan_service.extract_tags"
_NO_READ = AssertionError("read a file whose size and mtime are unchanged")


def _track(tmp_path: Path, name: str = "track.flac") -> Path:
    path = tmp_path / "jazz" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"\x00" * 100)
    return path


def _on_disk(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def _row(
    path: Path,
    *,
    stat: tuple[int, int] | None,
    status: FileStatus = FileStatus.PRESENT,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        file_status=status,
        work_id="w-1",
        file_size=stat[0] if stat else None,
        file_mtime_ns=stat[1] if stat else None,
    )


def _fresh(path: Path) -> LibraryFile:
    """What read_tags returns for a file: its stat, no links."""
    size, mtime_ns = _on_disk(path)
    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        format="flac",
        file_size=size,
        file_mtime_ns=mtime_ns,
    )


def _scan(path: Path, repo: FakeLibraryFileRepository) -> FolderScanResult:
    return scan_folder_incrementally(
        folder_path=path.parent,
        file_repo=repo,
        quarantine_repo=FakeLibraryQuarantineRepository(),
    )


def test_unchanged_file_is_skipped_unread(tmp_path: Path) -> None:
    path = _track(tmp_path)
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(path, stat=_on_disk(path)))

    with patch(_READ, side_effect=_NO_READ):
        result = _scan(path, repo)

    assert (result.files_skipped, result.files_written) == (1, 0)


def test_changed_size_rereads_tags_into_the_same_row(tmp_path: Path) -> None:
    path = _track(tmp_path)
    size, mtime_ns = _on_disk(path)
    repo = FakeLibraryFileRepository()
    stored = repo.upsert(_row(path, stat=(size + 1, mtime_ns)))

    with patch(_READ, return_value=_fresh(path)) as read:
        result = _scan(path, repo)

    got = repo.get_by_path(str(path))
    read.assert_called_once()
    assert result.files_written == 1
    assert got is not None and (got.id, got.work_id) == (stored.id, "w-1")
    assert got.enrichment_status == EnrichmentStatus.PENDING


def test_changed_mtime_alone_rereads_tags(tmp_path: Path) -> None:
    """A retag that fits inside the file's padding keeps its size."""
    path = _track(tmp_path)
    size, mtime_ns = _on_disk(path)
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(path, stat=(size, mtime_ns - 1_000_000_000)))

    with patch(_READ, return_value=_fresh(path)):
        result = _scan(path, repo)

    assert result.files_written == 1


def test_row_without_a_stored_stat_is_reread_even_if_older_than_its_index_row(
    tmp_path: Path,
) -> None:
    """A file restored from an archive keeps its old mtime; only a read tells."""
    path = _track(tmp_path)
    a_day_ago_ns = int((datetime.now(UTC) - timedelta(days=1)).timestamp() * 1e9)
    os.utime(path, ns=(a_day_ago_ns, a_day_ago_ns))
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(path, stat=None))

    with patch(_READ, return_value=_fresh(path)):
        result = _scan(path, repo)

    got = repo.get_by_path(str(path))
    assert result.files_written == 1
    assert got is not None and (got.file_size, got.file_mtime_ns) == _on_disk(path)


def test_new_file_is_inserted_pending(tmp_path: Path) -> None:
    path = _track(tmp_path)
    repo = FakeLibraryFileRepository()

    with patch(_READ, return_value=_fresh(path)):
        result = _scan(path, repo)

    got = repo.get_by_path(str(path))
    assert result.files_written == 1
    assert got is not None and got.enrichment_status == EnrichmentStatus.PENDING


def test_reappeared_file_with_unchanged_stat_is_restored_unread(tmp_path: Path) -> None:
    path = _track(tmp_path)
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(path, stat=_on_disk(path)))
    repo.mark_missing(str(path))

    with patch(_READ, side_effect=_NO_READ):
        result = _scan(path, repo)

    got = repo.get_by_path(str(path))
    assert (result.files_reappeared, result.files_written) == (1, 0)
    assert got is not None and got.file_status == FileStatus.PRESENT
    assert got.enrichment_status == EnrichmentStatus.ENRICHED


def test_reappeared_file_with_changed_stat_is_reread_into_the_same_row(tmp_path: Path) -> None:
    path = _track(tmp_path)
    size, mtime_ns = _on_disk(path)
    repo = FakeLibraryFileRepository()
    stored = repo.upsert(_row(path, stat=(size + 1, mtime_ns)))
    repo.mark_missing(str(path))

    with patch(_READ, return_value=_fresh(path)):
        result = _scan(path, repo)

    got = repo.get_by_path(str(path))
    assert (result.files_reappeared, result.files_written) == (1, 1)
    assert got is not None and (got.id, got.work_id) == (stored.id, "w-1")
    assert got.file_status == FileStatus.PRESENT
    assert got.enrichment_status == EnrichmentStatus.PENDING


def test_missing_file_is_marked(tmp_path: Path) -> None:
    folder = tmp_path / "jazz"
    folder.mkdir()
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(folder / "ghost.flac", stat=(1, 1)))

    result = _scan(folder / "ghost.flac", repo)

    got = repo.get_by_path(str(folder / "ghost.flac"))
    assert result.files_missing == 1
    assert got is not None and got.file_status == FileStatus.MISSING


def test_parse_failure_is_quarantined(tmp_path: Path) -> None:
    path = _track(tmp_path, "corrupt.flac")
    q_repo = FakeLibraryQuarantineRepository()

    with patch(_READ, side_effect=MutagenError("bad file")):
        result = scan_folder_incrementally(
            folder_path=path.parent,
            file_repo=FakeLibraryFileRepository(),
            quarantine_repo=q_repo,
        )

    assert result.quarantined == 1
    assert q_repo.get_by_path(str(path)) is not None


def test_a_file_that_cannot_be_statted_is_recorded_as_failing(tmp_path: Path) -> None:
    path = _track(tmp_path)
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(path, stat=_on_disk(path)))

    with patch(_STAT, side_effect=PermissionError("denied")):
        result = _scan(path, repo)

    assert result.quarantined == 1
    assert str(path) in result.failing_paths


def test_no_scenario_reads_a_whole_file(tmp_path: Path) -> None:
    new = _track(tmp_path, "new.flac")
    changed = _track(tmp_path, "changed.flac")
    repo = FakeLibraryFileRepository()
    size, mtime_ns = _on_disk(changed)
    repo.upsert(_row(changed, stat=(size + 1, mtime_ns)))

    with (
        patch(_WHOLE_FILE, side_effect=AssertionError("whole-file read"), create=True),
        patch(_READ, side_effect=lambda p: _fresh(p)),
    ):
        result = _scan(new, repo)

    assert (result.files_written, result.quarantined) == (2, 0)
