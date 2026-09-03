"""Unit tests for folder_hash_service."""
from __future__ import annotations

import hashlib
from pathlib import Path

from backend.services.folder_hash_service import (
    canonicalize_path,
    compute_folder_hash,
    diff_tree,
)
from tests.fakes.library_folders import FakeLibraryFolderRepository


class TestComputeFolderHash:
    def test_empty_folder(self, tmp_path: Path) -> None:
        result = compute_folder_hash(tmp_path)
        expected = hashlib.sha256(b"").hexdigest()
        assert result == expected

    def test_hash_changes_when_file_added(self, tmp_path: Path) -> None:
        h1 = compute_folder_hash(tmp_path)
        (tmp_path / "track.flac").write_bytes(b"\x00" * 100)
        h2 = compute_folder_hash(tmp_path)
        assert h1 != h2

    def test_hash_stable_for_same_content(self, tmp_path: Path) -> None:
        (tmp_path / "track.flac").write_bytes(b"\x00" * 100)
        h1 = compute_folder_hash(tmp_path)
        h2 = compute_folder_hash(tmp_path)
        assert h1 == h2

    def test_hash_changes_when_size_changes(self, tmp_path: Path) -> None:
        f = tmp_path / "track.flac"
        f.write_bytes(b"\x00" * 100)
        h1 = compute_folder_hash(tmp_path)
        f.write_bytes(b"\x00" * 200)
        h2 = compute_folder_hash(tmp_path)
        assert h1 != h2

    def test_ignores_non_audio_files(self, tmp_path: Path) -> None:
        (tmp_path / "readme.txt").write_text("hello")
        (tmp_path / "track.flac").write_bytes(b"\x00" * 100)
        h1 = compute_folder_hash(tmp_path)
        (tmp_path / "cover.jpg").write_bytes(b"\xff\xd8" * 50)
        h2 = compute_folder_hash(tmp_path)
        assert h1 == h2

    def test_ignores_changes_inside_subfolders(self, tmp_path: Path) -> None:
        """A folder's hash covers only its own audio files, never a child's.

        The targeted scan is non-recursive, so a parent must not light up
        when only a child changed — that would send the scanner to a folder
        with nothing to do and leave the real change unindexed.
        """
        sub = tmp_path / "jazz"
        sub.mkdir()
        h1 = compute_folder_hash(tmp_path)
        (sub / "track.flac").write_bytes(b"\x00" * 100)
        assert compute_folder_hash(tmp_path) == h1


class TestCanonicalizePath:
    def test_normpath(self) -> None:
        result = canonicalize_path("/music//jazz/../jazz/")
        assert "//" not in result
        assert result.endswith("jazz")

    def test_strips_trailing_slash(self) -> None:
        a = canonicalize_path("/music/jazz")
        b = canonicalize_path("/music/jazz/")
        assert a == b


class TestDiffTree:
    def test_first_run_returns_empty_changes(self, tmp_path: Path) -> None:
        sub = tmp_path / "jazz"
        sub.mkdir()
        (sub / "track.flac").write_bytes(b"\x00" * 100)

        repo = FakeLibraryFolderRepository()
        changes, pending = diff_tree(str(tmp_path), repo)

        assert changes == []
        assert repo.has_any() is True

    def test_detects_new_file(self, tmp_path: Path) -> None:
        sub = tmp_path / "jazz"
        sub.mkdir()
        (sub / "track.flac").write_bytes(b"\x00" * 100)

        repo = FakeLibraryFolderRepository()
        diff_tree(str(tmp_path), repo)

        (sub / "track2.flac").write_bytes(b"\x00" * 200)

        changes, pending = diff_tree(str(tmp_path), repo)
        assert len(changes) > 0
        assert any("jazz" in p for p in changes)

    def test_no_changes_returns_empty(self, tmp_path: Path) -> None:
        sub = tmp_path / "jazz"
        sub.mkdir()
        (sub / "track.flac").write_bytes(b"\x00" * 100)

        repo = FakeLibraryFolderRepository()
        diff_tree(str(tmp_path), repo)

        changes, pending = diff_tree(str(tmp_path), repo)
        assert changes == []

    def test_change_in_leaf_reports_only_that_leaf(self, tmp_path: Path) -> None:
        """Ancestors are not reported.

        This is the regression that made the watcher useless on a real
        library: a Merkle-style hash marked every ancestor up to the root as
        changed, the list collapsed to the root, and the non-recursive scan
        of the root indexed nothing while committing the new baseline.
        """
        miles = tmp_path / "jazz" / "miles"
        miles.mkdir(parents=True)
        (miles / "so_what.flac").write_bytes(b"\x00" * 100)

        repo = FakeLibraryFolderRepository()
        diff_tree(str(tmp_path), repo)

        (miles / "blue.flac").write_bytes(b"\x00" * 200)

        changes, _ = diff_tree(str(tmp_path), repo)
        assert changes == [canonicalize_path(str(miles))]

    def test_folder_with_null_baseline_is_reported_changed(self, tmp_path: Path) -> None:
        """NULL stored hash means "never verified under the current scheme".

        The migration that switched to files-only hashing nulls every
        baseline so each folder is re-checked exactly once, then settles.
        """
        jazz = tmp_path / "jazz"
        jazz.mkdir()
        (jazz / "track.flac").write_bytes(b"\x00" * 100)

        repo = FakeLibraryFolderRepository()
        diff_tree(str(tmp_path), repo)

        folder = repo.get_by_path(canonicalize_path(str(jazz)))
        assert folder is not None
        folder.folder_hash = None

        changes, _ = diff_tree(str(tmp_path), repo)
        assert canonicalize_path(str(jazz)) in changes

    def test_nonexistent_root_returns_empty(self) -> None:
        repo = FakeLibraryFolderRepository()
        changes, pending = diff_tree("/nonexistent/path", repo)
        assert changes == []
        assert pending == []

    def test_skips_folders_with_in_flight_staged_hashes(
        self, tmp_path: Path,
    ) -> None:
        """Folders with uncommitted staged hashes are excluded from both
        ``changed`` and ``pending`` to prevent duplicate staging across
        overlapping poll cycles.
        """
        sub = tmp_path / "jazz"
        sub.mkdir()
        (sub / "track.flac").write_bytes(b"\x00" * 100)

        repo = FakeLibraryFolderRepository()
        # First run seeds the DB
        diff_tree(str(tmp_path), repo)

        jazz_folder = repo.get_by_path(canonicalize_path(str(sub)))
        assert jazz_folder is not None
        repo.stage_hashes(
            [(jazz_folder.id, "fake_in_flight_hash")], "in_flight_task",
        )

        # Now add a new file to the jazz directory
        (sub / "track2.flac").write_bytes(b"\x00" * 200)

        # The jazz folder should be skipped because it has staged hashes
        in_flight_ids = repo.get_folders_with_staged_hashes()
        changes, pending = diff_tree(str(tmp_path), repo, in_flight_ids)

        # jazz folder should NOT appear in changed or pending
        assert jazz_folder.id not in {fid for fid, _ in pending}
        assert not any("jazz" in p for p in changes)
