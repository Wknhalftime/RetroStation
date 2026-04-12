"""Unit tests for library_watcher_poll and library_scan_files_task."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from backend.domain.system import UserSetting


class TestWatcherPollNoPath:
    @patch("backend.tasks.library_watcher_tasks.connect_sync")
    def test_noop_when_no_path_setting_absent(self, mock_connect: MagicMock) -> None:
        """Poll exits early when local_path_prefix is not set (returns None)."""
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        mock_settings = MagicMock()
        mock_settings.get.return_value = None  # key absent → UserSetting | None

        with patch("backend.tasks.library_watcher_tasks.RepositoryFactory") as mock_factory:
            mock_factory.return_value.settings = mock_settings
            with patch("backend.tasks.library_watcher_tasks.diff_tree") as mock_diff:
                from backend.tasks.library_watcher_tasks import library_watcher_poll
                library_watcher_poll.call_local()
                mock_diff.assert_not_called()

    @patch("backend.tasks.library_watcher_tasks.connect_sync")
    def test_noop_when_path_setting_has_empty_value(self, mock_connect: MagicMock) -> None:
        """Poll exits early when local_path_prefix is set but its value is empty."""
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        mock_settings = MagicMock()
        mock_settings.get.return_value = UserSetting(key="local_path_prefix", value="")

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
        # get() must return a UserSetting so .value unwrap in the task succeeds
        mock_settings.get.return_value = UserSetting(key="local_path_prefix", value="/music")

        with (
            patch("backend.tasks.library_watcher_tasks.RepositoryFactory") as mock_factory,
            patch("backend.tasks.library_watcher_tasks.diff_tree", return_value=([], [])),
            patch("backend.tasks.library_watcher_tasks.library_scan_files_task") as mock_scan,
        ):
            mock_factory.return_value.settings = mock_settings
            from backend.tasks.library_watcher_tasks import library_watcher_poll
            library_watcher_poll.call_local()
            mock_scan.assert_not_called()


class TestWatcherPollUsesSettingValue:
    """Prove that the .value unwrap works end-to-end using FakeUserSettingRepository."""

    @patch("backend.tasks.library_watcher_tasks.connect_sync")
    @patch("backend.tasks.library_watcher_tasks.diff_tree", return_value=([], []))
    def test_reads_path_from_user_setting(
        self, mock_diff: MagicMock, mock_connect: MagicMock
    ) -> None:
        """When local_path_prefix has a value, diff_tree is reached (no early return)."""
        from tests.fakes.user_settings import FakeUserSettingRepository

        mock_conn = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)

        fake_settings = FakeUserSettingRepository(initial={"local_path_prefix": "/music"})

        with patch("backend.tasks.library_watcher_tasks.RepositoryFactory") as mock_factory:
            mock_factory.return_value.settings = fake_settings
            mock_factory.return_value.library_folders = MagicMock()

            from backend.tasks.library_watcher_tasks import library_watcher_poll
            library_watcher_poll.call_local()

            # If .value unwrap was wrong the task would have returned early;
            # reaching diff_tree proves the code path is correct.
            mock_diff.assert_called_once_with("/music", mock_factory.return_value.library_folders)
