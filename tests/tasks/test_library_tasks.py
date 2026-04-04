"""Tests for the incremental-write and chunked-commit behavior of _run_scan."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from backend.domain.enums import EnrichmentStatus, TaskStatus
from backend.domain.models import LibraryFile, LibraryQuarantine


def _make_lf(idx: int) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/tmp/test_{idx}.mp3",
        file_hash="a" * 64,
        format="mp3",
        enrichment_status=EnrichmentStatus.PENDING,
    )


def _make_q(idx: int) -> LibraryQuarantine:
    return LibraryQuarantine(
        id=uuid4(),
        file_path=f"/tmp/bad_{idx}.mp3",
        error_message="bad file",
    )


class TestRunScanChunkedCommits:
    """Test that _run_scan commits at COMMIT_CHUNK_SIZE boundaries."""

    @patch("backend.tasks.library_tasks.scan_directory")
    def test_commit_fires_at_chunk_boundary(self, mock_scan: MagicMock) -> None:
        """With chunk_size=3 and 5 files, commit should fire at file 3 and at end."""
        from backend.tasks.library_tasks import _run_scan

        files = [_make_lf(i) for i in range(5)]

        def fake_scan(root: Path, **kwargs):
            on_file = kwargs["on_file"]
            for lf in files:
                on_file(lf)
            return files, []

        mock_scan.side_effect = fake_scan

        mock_conn = MagicMock()
        mock_repos = MagicMock()

        _run_scan(
            root_path="/tmp/music",
            library_conn=mock_conn,
            repos=mock_repos,
            progress_repo=MagicMock(),
            task_id="test123",
            chunk_size=3,
        )

        # commit at file 3 (chunk boundary) + commit for remaining 2 at end = 2 commits
        assert mock_conn.commit.call_count == 2

    @patch("backend.tasks.library_tasks.scan_directory")
    def test_commit_fires_once_when_exact_chunk(self, mock_scan: MagicMock) -> None:
        """With chunk_size=3 and exactly 3 files, commit at boundary + no trailing = 1 commit."""
        from backend.tasks.library_tasks import _run_scan

        files = [_make_lf(i) for i in range(3)]

        def fake_scan(root: Path, **kwargs):
            on_file = kwargs["on_file"]
            for lf in files:
                on_file(lf)
            return files, []

        mock_scan.side_effect = fake_scan

        mock_conn = MagicMock()
        mock_repos = MagicMock()

        _run_scan(
            root_path="/tmp/music",
            library_conn=mock_conn,
            repos=mock_repos,
            progress_repo=MagicMock(),
            task_id="test123",
            chunk_size=3,
        )

        # Exactly at boundary: commit fires at 3, pending_writes resets to 0,
        # trailing commit is skipped because pending_writes == 0
        assert mock_conn.commit.call_count == 1

    @patch("backend.tasks.library_tasks.scan_directory")
    def test_upsert_write_only_called_for_each_file(self, mock_scan: MagicMock) -> None:
        """Each file should trigger upsert_write_only, not the old upsert."""
        from backend.tasks.library_tasks import _run_scan

        files = [_make_lf(i) for i in range(3)]

        def fake_scan(root: Path, **kwargs):
            on_file = kwargs["on_file"]
            for lf in files:
                on_file(lf)
            return files, []

        mock_scan.side_effect = fake_scan

        mock_conn = MagicMock()
        mock_repos = MagicMock()

        _run_scan(
            root_path="/tmp/music",
            library_conn=mock_conn,
            repos=mock_repos,
            progress_repo=MagicMock(),
            task_id="test123",
            chunk_size=100,
        )

        assert mock_repos.library_files.upsert_write_only.call_count == 3
        # The old upsert should NOT be called
        mock_repos.library_files.upsert.assert_not_called()

    @patch("backend.tasks.library_tasks.scan_directory")
    def test_create_write_only_called_for_quarantine(self, mock_scan: MagicMock) -> None:
        """Quarantine entries should use create_write_only."""
        from backend.tasks.library_tasks import _run_scan

        quarantine = [_make_q(i) for i in range(2)]

        def fake_scan(root: Path, **kwargs):
            on_quarantine = kwargs["on_quarantine"]
            for q in quarantine:
                on_quarantine(q)
            return [], quarantine

        mock_scan.side_effect = fake_scan

        mock_conn = MagicMock()
        mock_repos = MagicMock()

        _run_scan(
            root_path="/tmp/music",
            library_conn=mock_conn,
            repos=mock_repos,
            progress_repo=MagicMock(),
            task_id="test123",
            chunk_size=100,
        )

        assert mock_repos.library_quarantine.create_write_only.call_count == 2
        mock_repos.library_quarantine.create.assert_not_called()

    @patch("backend.tasks.library_tasks.scan_directory")
    def test_mixed_files_and_quarantine_share_chunk_counter(
        self, mock_scan: MagicMock
    ) -> None:
        """Files and quarantine entries both count toward the chunk boundary."""
        from backend.tasks.library_tasks import _run_scan

        def fake_scan(root: Path, **kwargs):
            on_file = kwargs["on_file"]
            on_quarantine = kwargs["on_quarantine"]
            # 2 files + 1 quarantine = 3 writes = chunk boundary at chunk_size=3
            on_file(_make_lf(0))
            on_file(_make_lf(1))
            on_quarantine(_make_q(0))
            return [_make_lf(0), _make_lf(1)], [_make_q(0)]

        mock_scan.side_effect = fake_scan

        mock_conn = MagicMock()
        mock_repos = MagicMock()

        _run_scan(
            root_path="/tmp/music",
            library_conn=mock_conn,
            repos=mock_repos,
            progress_repo=MagicMock(),
            task_id="test123",
            chunk_size=3,
        )

        # Exactly at boundary, so 1 commit (no trailing)
        assert mock_conn.commit.call_count == 1

    @patch("backend.tasks.library_tasks.scan_directory")
    def test_returns_counts_and_progress(self, mock_scan: MagicMock) -> None:
        """_run_scan should return (files_written, quarantine_written, last_progress)."""
        from backend.tasks.library_tasks import _run_scan

        def fake_scan(root: Path, **kwargs):
            on_file = kwargs["on_file"]
            on_quarantine = kwargs["on_quarantine"]
            on_file(_make_lf(0))
            on_file(_make_lf(1))
            on_quarantine(_make_q(0))
            return [_make_lf(0), _make_lf(1)], [_make_q(0)]

        mock_scan.side_effect = fake_scan

        files_written, quarantine_written, last_progress = _run_scan(
            root_path="/tmp/music",
            library_conn=MagicMock(),
            repos=MagicMock(),
            progress_repo=MagicMock(),
            task_id="test123",
            chunk_size=100,
        )

        assert files_written == 2
        assert quarantine_written == 1
        assert isinstance(last_progress, dict)

    @patch("backend.tasks.library_tasks.scan_directory")
    def test_on_progress_updates_progress_repo(self, mock_scan: MagicMock) -> None:
        """on_progress callback should upsert into progress_repo."""
        from backend.tasks.library_tasks import _run_scan

        def fake_scan(root: Path, **kwargs):
            on_progress = kwargs["on_progress"]
            on_progress(50, 100, "/music/track_50.mp3")
            on_progress(100, 100, "/music/track_100.mp3")
            return ([], [])

        mock_scan.side_effect = fake_scan

        mock_conn = MagicMock()
        mock_repos = MagicMock()
        mock_progress_repo = MagicMock()

        files_written, quarantine_written, last_progress = _run_scan(
            root_path="/music",
            library_conn=mock_conn,
            repos=mock_repos,
            progress_repo=mock_progress_repo,
            task_id="test-progress-task",
            chunk_size=100,
        )

        # progress_repo.upsert called twice (once per on_progress call)
        assert mock_progress_repo.upsert.call_count == 2

        # Verify the last call's ProgressTracking shape
        last_call_arg = mock_progress_repo.upsert.call_args_list[-1][0][0]
        assert last_call_arg.task_id == "test-progress-task"
        assert last_call_arg.status == TaskStatus.RUNNING
        assert last_call_arg.progress_data["processed"] == 100
        assert last_call_arg.progress_data["total"] == 100
        assert last_call_arg.progress_data["current_path"] == "/music/track_100.mp3"

        # last_progress return value should match
        assert last_progress["processed"] == 100
        assert last_progress["total"] == 100
