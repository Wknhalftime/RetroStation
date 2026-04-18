"""Tests for scan_directory progress callback."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.domain.enums import TaskStatus, TaskType
from backend.services.library_scan_service import scan_directory
from tests.fakes.task_progress import FakeTaskProgressRepository

AUDIO_DIR = Path(__file__).parent.parent / "fixtures" / "audio"
NO_TAGS_WAV = AUDIO_DIR / "no_tags.wav"


def _require_wav() -> Path:
    if not NO_TAGS_WAV.exists():
        pytest.skip("Fixture not found: no_tags.wav")
    return NO_TAGS_WAV


class TestScanDirectoryProgress:
    def test_callback_fires_with_correct_total(self, tmp_path: Path) -> None:
        """Callback receives total == number of supported audio files."""
        wav = _require_wav()
        for i in range(3):
            shutil.copy(wav, tmp_path / f"track_{i}.wav")

        calls: list[tuple[int, int, str]] = []
        scan_directory(tmp_path, on_progress=lambda p, t, c: calls.append((p, t, c)))

        # With 3 files (< 50), callback fires once on the last file
        assert len(calls) == 1
        processed, total, _ = calls[0]
        assert total == 3
        assert processed == 3

    def test_callback_fires_every_50_files(self, tmp_path: Path) -> None:
        """Callback fires at file 50 and at the end for 75 files.

        Empty files suffice: callback cadence counts enumerate()-index, not
        successful extractions, so MutagenError-quarantined files still
        advance the counter and exercise the every-50 branch.
        """
        for i in range(75):
            (tmp_path / f"track_{i:03d}.wav").touch()

        calls: list[tuple[int, int, str]] = []
        scan_directory(tmp_path, on_progress=lambda p, t, c: calls.append((p, t, c)))

        assert len(calls) == 2  # at 50 and at 75
        assert calls[0][0] == 50
        assert calls[0][1] == 75
        assert calls[1][0] == 75
        assert calls[1][1] == 75

    def test_callback_current_path_is_string(self, tmp_path: Path) -> None:
        wav = _require_wav()
        shutil.copy(wav, tmp_path / "single.wav")

        calls: list[tuple[int, int, str]] = []
        scan_directory(tmp_path, on_progress=lambda p, t, c: calls.append((p, t, c)))

        assert len(calls) == 1
        assert isinstance(calls[0][2], str)
        assert "single.wav" in calls[0][2]

    def test_no_callback_is_fine(self, tmp_path: Path) -> None:
        """scan_directory still works without on_progress."""
        wav = _require_wav()
        shutil.copy(wav, tmp_path / "track.wav")

        files, quarantine = scan_directory(tmp_path)
        assert len(files) == 1
        assert len(quarantine) == 0

    def test_results_unchanged_with_callback(self, tmp_path: Path) -> None:
        """Adding a callback does not change the returned files/quarantine."""
        wav = _require_wav()
        for i in range(3):
            shutil.copy(wav, tmp_path / f"track_{i}.wav")

        files_no_cb, q_no_cb = scan_directory(tmp_path)
        files_cb, q_cb = scan_directory(
            tmp_path, on_progress=lambda p, t, c: None
        )

        assert len(files_cb) == len(files_no_cb)
        assert len(q_cb) == len(q_no_cb)

    def test_candidates_are_sorted(self, tmp_path: Path) -> None:
        """Files are processed in sorted order (deterministic)."""
        wav = _require_wav()
        shutil.copy(wav, tmp_path / "z_last.wav")
        shutil.copy(wav, tmp_path / "a_first.wav")

        files, _ = scan_directory(tmp_path)
        file_names = [Path(f.file_path).name for f in files]
        assert file_names == sorted(file_names)


class TestLibraryScanTaskProgress:
    """Tests for library_scan_task progress tracking lifecycle."""

    @patch("backend.tasks.library_scan_tasks.psycopg")
    @patch("backend.tasks.library_scan_tasks.scan_directory")
    def test_running_record_exists_before_scan_starts(
        self, mock_scan: MagicMock, mock_psycopg: MagicMock
    ) -> None:
        """Verify RUNNING is written before scan_directory is called."""
        from backend.tasks.library_scan_tasks import library_scan_task

        fake_progress = FakeTaskProgressRepository()
        status_at_scan_time: list[TaskStatus] = []

        def capture_status_then_return_empty(
            *args: object, **kwargs: object,
        ) -> tuple[list[object], list[object]]:
            # When scan_directory is called, capture current progress status
            records = list(fake_progress._data.values())
            if records:
                status_at_scan_time.append(records[0].status)
            return ([], [])

        mock_scan.side_effect = capture_status_then_return_empty

        mock_autocommit_conn = MagicMock()
        mock_data_conn = MagicMock()
        mock_data_conn.__enter__ = MagicMock(return_value=mock_data_conn)
        mock_data_conn.__exit__ = MagicMock(return_value=False)

        def connect_side_effect(*args: object, **kwargs: object) -> object:
            if kwargs.get("autocommit"):
                return mock_autocommit_conn
            return mock_data_conn

        mock_psycopg.connect.side_effect = connect_side_effect

        with patch(
            "backend.tasks.library_scan_tasks.PgTaskProgressRepository",
            return_value=fake_progress,
        ):
            library_scan_task.call_local("/fake/path")

        # RUNNING was visible when scan_directory was invoked
        assert len(status_at_scan_time) == 1
        assert status_at_scan_time[0] == TaskStatus.RUNNING

        # Final state is COMPLETED
        records = list(fake_progress._data.values())
        assert len(records) == 1
        assert records[0].status == TaskStatus.COMPLETED
        assert records[0].task_type == TaskType.SCAN

    @patch("backend.tasks.library_scan_tasks.psycopg")
    @patch("backend.tasks.library_scan_tasks.scan_directory")
    def test_marks_failed_on_scan_exception(
        self, mock_scan: MagicMock, mock_psycopg: MagicMock
    ) -> None:
        from backend.tasks.library_scan_tasks import library_scan_task

        fake_progress = FakeTaskProgressRepository()
        mock_scan.side_effect = RuntimeError("disk error")

        mock_autocommit_conn = MagicMock()
        mock_psycopg.connect.return_value = mock_autocommit_conn

        with (
            patch(
                "backend.tasks.library_scan_tasks.PgTaskProgressRepository",
                return_value=fake_progress,
            ),
            pytest.raises(RuntimeError, match="disk error"),
        ):
            library_scan_task.call_local("/fake/path")

        records = list(fake_progress._data.values())
        assert len(records) == 1
        assert records[0].status == TaskStatus.FAILED
        assert "disk error" in records[0].progress_data.get("error", "")

    @patch("backend.tasks.library_scan_tasks.psycopg")
    @patch("backend.tasks.library_scan_tasks.scan_directory")
    def test_progress_data_has_processed_and_total(
        self, mock_scan: MagicMock, mock_psycopg: MagicMock
    ) -> None:
        from backend.tasks.library_scan_tasks import library_scan_task

        fake_progress = FakeTaskProgressRepository()
        mock_scan.return_value = ([], [])

        mock_autocommit_conn = MagicMock()
        mock_data_conn = MagicMock()
        mock_data_conn.__enter__ = MagicMock(return_value=mock_data_conn)
        mock_data_conn.__exit__ = MagicMock(return_value=False)

        def connect_side_effect(*args: object, **kwargs: object) -> object:
            if kwargs.get("autocommit"):
                return mock_autocommit_conn
            return mock_data_conn

        mock_psycopg.connect.side_effect = connect_side_effect

        with patch(
            "backend.tasks.library_scan_tasks.PgTaskProgressRepository",
            return_value=fake_progress,
        ):
            library_scan_task.call_local("/fake/path")

        records = list(fake_progress._data.values())
        assert "processed" in records[0].progress_data
        assert "total" in records[0].progress_data
