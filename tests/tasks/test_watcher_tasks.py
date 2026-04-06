"""Unit tests for library_watcher_poll and library_scan_files_task."""
from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestWatcherPollNoPath:
    @patch("backend.tasks.library_watcher_tasks.connect_sync")
    def test_noop_when_no_path(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        mock_settings = MagicMock()
        mock_settings.get.return_value = None

        with patch("backend.tasks.library_watcher_tasks.RepositoryFactory") as mock_factory:
            mock_factory.return_value.settings = mock_settings
            with patch("backend.tasks.library_watcher_tasks.diff_tree") as mock_diff:
                from backend.tasks.library_watcher_tasks import library_watcher_poll
                library_watcher_poll.call_local()
                mock_diff.assert_not_called()


class TestWatcherPollNoChanges:
    @patch("backend.tasks.library_watcher_tasks.connect_sync")
    def test_no_scan_when_no_changes(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        mock_settings = MagicMock()
        mock_settings.get.return_value = "/music"

        with patch("backend.tasks.library_watcher_tasks.RepositoryFactory") as mock_factory:
            mock_factory.return_value.settings = mock_settings
            with patch("backend.tasks.library_watcher_tasks.diff_tree", return_value=([], [])):
                with patch("backend.tasks.library_watcher_tasks.library_scan_files_task") as mock_scan:
                    from backend.tasks.library_watcher_tasks import library_watcher_poll
                    library_watcher_poll.call_local()
                    mock_scan.assert_not_called()
