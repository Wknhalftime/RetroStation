"""AUD-009 gate 1: branch coverage for the reconcile half of library_scan_service.

Targets the branches audit/triage/findings.jsonl AUD-009 names as uncovered
before the split: the already-MISSING skip in ``_mark_missing_files``, the
``_reread_and_upsert`` no-op exit, ``_is_same_file``'s OSError path (and the
``_respelled_from`` loop continuing past a stale candidate), the
non-case-only branch of ``_spelled_as_on_disk``, ``_move_candidates``'
stat-lookup skip, ``_deduplicated_by_id``'s duplicate-drop, a populated
``mark_unseen_missing`` walk, and ``_restore_reappeared_file``'s stat-failure
exit. These functions stay in library_scan_service.py after the split.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from mutagen._util import MutagenError

from backend.domain.enums import AudioHashKind, FileStatus
from backend.domain.library import AudioHash, LibraryFile
from backend.services.library_scan_service import (
    _deduplicated_by_id,
    _spelled_as_on_disk,
    adopt_moved_row,
    mark_unseen_missing,
    scan_folder_incrementally,
)
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.library_quarantine import FakeLibraryQuarantineRepository

H1 = AudioHash(AudioHashKind.FLAC_MD5, "1" * 32)

_READ = "backend.services.library_scan_service.read_tags"
_STAT = "backend.services.library_scan_service.disk_stat"


def _write(path: Path, data: bytes = b"\x00" * 100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _row(
    path: Path,
    *,
    status: FileStatus = FileStatus.PRESENT,
    stat: tuple[int, int] | None = (1, 1),
    audio_hash: AudioHash | None = None,
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        format="flac",
        file_status=status,
        file_size=stat[0] if stat else None,
        file_mtime_ns=stat[1] if stat else None,
        audio_hash=audio_hash,
    )


def _fresh(path: Path) -> LibraryFile:
    st = path.stat()
    return LibraryFile(
        id=uuid4(),
        file_path=str(path),
        format="flac",
        file_size=st.st_size,
        file_mtime_ns=st.st_mtime_ns,
    )


# ---------------------------------------------------------------------------
# mark_unseen_missing — a populated walk (previously never exercised)
# ---------------------------------------------------------------------------


def test_mark_unseen_missing_marks_only_present_unseen_files(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    seen = root / "seen.flac"
    gone = root / "gone.flac"
    already_missing = root / "already_missing.flac"
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(seen))
    repo.upsert(_row(gone))
    repo.upsert(_row(already_missing, status=FileStatus.MISSING))

    marked = mark_unseen_missing(root, {str(seen)}, repo)

    assert marked == 1
    assert repo.get_by_path(str(seen)).file_status == FileStatus.PRESENT  # type: ignore[union-attr]
    assert repo.get_by_path(str(gone)).file_status == FileStatus.MISSING  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# _mark_missing_files — a row already MISSING is not touched or re-counted
# ---------------------------------------------------------------------------


def test_folder_scan_does_not_recount_a_row_already_missing(tmp_path: Path) -> None:
    folder = tmp_path / "jazz"
    folder.mkdir()
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(folder / "gone.flac"))
    repo.upsert(_row(folder / "long_gone.flac", status=FileStatus.MISSING))

    result = scan_folder_incrementally(
        folder_path=folder,
        file_repo=repo,
        quarantine_repo=FakeLibraryQuarantineRepository(),
    )

    assert result.files_missing == 1
    assert repo.get_by_path(str(folder / "long_gone.flac")).file_status == (  # type: ignore[union-attr]
        FileStatus.MISSING
    )


# ---------------------------------------------------------------------------
# _reread_and_upsert — a reread that fails writes nothing
# ---------------------------------------------------------------------------


def test_a_failed_reread_of_a_changed_file_writes_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "jazz"
    path = folder / "track.flac"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 100)
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(path, stat=(999, 999)))  # stored stat no longer matches disk

    with patch(_READ, side_effect=MutagenError("bad file")):
        result = scan_folder_incrementally(
            folder_path=folder,
            file_repo=repo,
            quarantine_repo=FakeLibraryQuarantineRepository(),
        )

    assert result.files_written == 0
    assert result.quarantined == 1


# ---------------------------------------------------------------------------
# _restore_reappeared_file — a stat failure on the reappeared path is a no-op
# ---------------------------------------------------------------------------


def test_a_reappeared_file_that_cannot_be_statted_is_left_alone(tmp_path: Path) -> None:
    folder = tmp_path / "jazz"
    path = folder / "track.flac"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 100)
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(path, status=FileStatus.MISSING))

    with patch(_STAT, side_effect=PermissionError("denied")):
        result = scan_folder_incrementally(
            folder_path=folder,
            file_repo=repo,
            quarantine_repo=FakeLibraryQuarantineRepository(),
        )

    assert result.files_reappeared == 0
    assert result.quarantined == 1
    assert repo.get_by_path(str(path)).file_status == FileStatus.MISSING  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# _is_same_file / _respelled_from — os.path.samefile is mocked here because
# NTFS is case-insensitive: two really-different files cannot share a path
# that differs only by case, so there is no way to put a case-differing
# candidate on disk whose samefile check fails, or errors, without mocking
# the underlying call.
# ---------------------------------------------------------------------------


def test_a_samefile_error_is_treated_as_not_the_same_file(tmp_path: Path) -> None:
    new = _write(tmp_path / "Prince" / "Song.flac")
    candidate = tmp_path / "Prince" / "song.flac"  # case-differing spelling of the same path
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(candidate))  # stat (1, 1): does not match new's real stat either

    with patch("os.path.samefile", side_effect=OSError("stat failed")):
        adopted = adopt_moved_row(_fresh(new), repo)

    assert adopted is None


def test_respelled_from_continues_past_a_non_matching_candidate(tmp_path: Path) -> None:
    new = _write(tmp_path / "Prince" / "Song.flac")
    # "SONG.flac" sorts before "song.flac" (case-sensitive), so
    # get_by_path_ignoring_case tries cand_a first.
    cand_a = tmp_path / "Prince" / "SONG.flac"
    cand_b = tmp_path / "Prince" / "song.flac"
    repo = FakeLibraryFileRepository()
    repo.upsert(_row(cand_a))
    stored_b = repo.upsert(_row(cand_b))

    with patch("os.path.samefile", side_effect=[False, True]):
        adopted_from = adopt_moved_row(_fresh(new), repo)

    assert adopted_from == str(cand_b)
    moved = repo.get_by_id(stored_b.id)
    assert moved is not None and moved.file_path == str(new)


# ---------------------------------------------------------------------------
# _spelled_as_on_disk — a real-path divergence beyond case (junction, subst)
# ---------------------------------------------------------------------------


def test_spelled_as_on_disk_accepts_a_non_case_divergence(tmp_path: Path) -> None:
    folder = tmp_path / "lib"
    folder.mkdir()
    with patch("os.path.realpath", return_value=str(tmp_path / "elsewhere")):
        assert _spelled_as_on_disk(folder) is True


# ---------------------------------------------------------------------------
# _move_candidates — no stat lookup without a complete stat
# ---------------------------------------------------------------------------


def test_a_move_candidate_found_by_hash_alone_needs_no_stat(tmp_path: Path) -> None:
    old = tmp_path / "unsorted" / "kiss.flac"
    new = _write(tmp_path / "Prince" / "kiss.flac")
    repo = FakeLibraryFileRepository()
    stored = repo.upsert(_row(old, audio_hash=H1, stat=None))
    repo.mark_missing(str(old))

    seen = _fresh(new)
    seen.file_size = None
    seen.file_mtime_ns = None
    seen.audio_hash = H1

    assert adopt_moved_row(seen, repo) == str(old)
    assert repo.get_by_id(stored.id).file_path == str(new)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# _deduplicated_by_id — a row found twice (hash and stat) counts once
# ---------------------------------------------------------------------------


def test_deduplicated_by_id_drops_a_row_seen_a_second_time() -> None:
    row = _row(Path("/lib/a.flac"), audio_hash=H1)
    assert _deduplicated_by_id([row, row]) == [row]
