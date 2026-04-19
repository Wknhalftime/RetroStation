"""Progress-emission tests for library_enrichment_task."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from backend.domain.enums import TaskStatus, TaskType


def _fake_connect_factory(
    release_rows: list[dict[str, str]] | None = None,
    recording_rows: list[dict[str, str]] | None = None,
) -> Any:
    """Build a connect_sync replacement.

    `connect_sync` returns a psycopg connection; callers use it either directly
    (assigned then `.close()`-ed in finally) or via `with connect_sync(...) as
    conn`. A MagicMock supports both because it already implements
    __enter__/__exit__ that return itself.
    """

    def fake_connect(_url: str, *, autocommit: bool = False) -> Any:
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False

        def execute(sql: str, *_args: Any) -> MagicMock:
            result = MagicMock()
            lowered = sql.lower()
            if "release_mbid is not null" in lowered:
                result.fetchall.return_value = release_rows or []
            elif "recording_mbid is not null" in lowered:
                result.fetchall.return_value = recording_rows or []
            else:
                result.fetchall.return_value = []
            return result

        conn.execute.side_effect = execute
        return conn

    return fake_connect


def _progress_calls(mock_repo: MagicMock) -> list[Any]:
    return [call.args[0] for call in mock_repo.upsert.call_args_list]


def _system_log_calls(mock_repo: MagicMock) -> list[Any]:
    return [call.args[0] for call in mock_repo.create.call_args_list]


class TestLibraryEnrichmentProgress:
    @patch("backend.tasks.mb_enrichment_tasks.mb_enrichment_task", MagicMock())
    @patch("backend.tasks.library_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.library_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks.library_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_enrichment_tasks.enrich_by_release", return_value=1)
    @patch("backend.tasks.library_enrichment_tasks.enrich_by_recording", return_value=1)
    @patch("backend.tasks.library_enrichment_tasks.connect_sync")
    def test_emits_running_on_start_with_correct_total(
        self,
        mock_connect: MagicMock,
        _enrich_recording: MagicMock,
        _enrich_release: MagicMock,
        mock_progress_cls: MagicMock,
        _sys_log_cls: MagicMock,
        _cache_cls: MagicMock,
        _repo_cls: MagicMock,
        _mb_cls: MagicMock,
    ) -> None:
        mock_connect.side_effect = _fake_connect_factory(
            release_rows=[{"release_mbid": "r1"}, {"release_mbid": "r2"}],
            recording_rows=[{"recording_mbid": "rec1"}],
        )
        mock_progress_repo = MagicMock()
        mock_progress_cls.return_value = mock_progress_repo

        from backend.tasks.library_enrichment_tasks import library_enrichment_task
        library_enrichment_task.call_local()

        calls = _progress_calls(mock_progress_repo)
        assert calls, "expected at least one progress upsert"
        first = calls[0]
        assert first.status == TaskStatus.RUNNING
        assert first.task_type == TaskType.LIBRARY_ENRICHMENT
        assert first.progress_data["total"] == 3
        assert first.progress_data["processed"] == 0

    @patch("backend.tasks.mb_enrichment_tasks.mb_enrichment_task", MagicMock())
    @patch("backend.tasks.library_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.library_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks.library_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_enrichment_tasks.enrich_by_release", return_value=1)
    @patch("backend.tasks.library_enrichment_tasks.enrich_by_recording", return_value=1)
    @patch("backend.tasks.library_enrichment_tasks.connect_sync")
    def test_processed_counter_monotonic_across_success_and_failure(
        self,
        mock_connect: MagicMock,
        _enrich_recording: MagicMock,
        mock_enrich_release: MagicMock,
        mock_progress_cls: MagicMock,
        _sys_log_cls: MagicMock,
        _cache_cls: MagicMock,
        _repo_cls: MagicMock,
        _mb_cls: MagicMock,
    ) -> None:
        mock_enrich_release.side_effect = [RuntimeError("boom"), 1]
        mock_connect.side_effect = _fake_connect_factory(
            release_rows=[{"release_mbid": "r1"}, {"release_mbid": "r2"}],
            recording_rows=[],
        )
        mock_progress_repo = MagicMock()
        mock_progress_cls.return_value = mock_progress_repo

        from backend.tasks.library_enrichment_tasks import library_enrichment_task
        library_enrichment_task.call_local()

        calls = _progress_calls(mock_progress_repo)
        running = [c for c in calls if c.status == TaskStatus.RUNNING]
        processed_values = [c.progress_data["processed"] for c in running]
        assert processed_values == sorted(processed_values)
        assert processed_values[-1] == 2  # both release iterations counted

    @patch("backend.tasks.mb_enrichment_tasks.mb_enrichment_task", MagicMock())
    @patch("backend.tasks.library_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.library_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks.library_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_enrichment_tasks.connect_sync")
    def test_zero_pending_work_still_emits_running_then_completed(
        self,
        mock_connect: MagicMock,
        mock_progress_cls: MagicMock,
        _sys_log_cls: MagicMock,
        _cache_cls: MagicMock,
        _repo_cls: MagicMock,
        _mb_cls: MagicMock,
    ) -> None:
        mock_connect.side_effect = _fake_connect_factory()
        mock_progress_repo = MagicMock()
        mock_progress_cls.return_value = mock_progress_repo

        from backend.tasks.library_enrichment_tasks import library_enrichment_task
        library_enrichment_task.call_local()

        statuses = [c.status for c in _progress_calls(mock_progress_repo)]
        assert statuses == [TaskStatus.RUNNING, TaskStatus.COMPLETED]
        last = _progress_calls(mock_progress_repo)[-1]
        assert last.progress_data["total"] == 0
        assert last.progress_data["processed"] == 0

    @patch("backend.tasks.mb_enrichment_tasks.mb_enrichment_task", MagicMock())
    @patch("backend.tasks.library_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.library_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks.library_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_enrichment_tasks.connect_sync")
    def test_marks_failed_when_library_connect_raises(
        self,
        mock_connect: MagicMock,
        mock_progress_cls: MagicMock,
        mock_sys_log_cls: MagicMock,
        _cache_cls: MagicMock,
        _repo_cls: MagicMock,
        _mb_cls: MagicMock,
    ) -> None:
        # First call to connect_sync is for progress_conn (autocommit=True); succeed.
        # Second call is the library connection; raise.
        def fake(_url: str, *, autocommit: bool = False) -> Any:
            if autocommit:
                conn = MagicMock()
                conn.__enter__.return_value = conn
                conn.__exit__.return_value = False
                return conn
            raise RuntimeError("db down")

        mock_connect.side_effect = fake
        mock_progress_repo = MagicMock()
        mock_progress_cls.return_value = mock_progress_repo
        mock_sys_log = MagicMock()
        mock_sys_log_cls.return_value = mock_sys_log

        from backend.tasks.library_enrichment_tasks import library_enrichment_task
        with pytest.raises(RuntimeError):
            library_enrichment_task.call_local()

        statuses = [c.status for c in _progress_calls(mock_progress_repo)]
        assert TaskStatus.FAILED in statuses
        failed = next(
            c for c in _progress_calls(mock_progress_repo)
            if c.status == TaskStatus.FAILED
        )
        assert "db down" in failed.progress_data["error"]
        # Failure SystemLog includes trace_id matching the progress task_id
        log_entries = _system_log_calls(mock_sys_log)
        assert any(entry.trace_id == failed.task_id for entry in log_entries)

    @patch("backend.tasks.mb_enrichment_tasks.mb_enrichment_task", MagicMock())
    @patch("backend.tasks.library_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.library_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks.library_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_enrichment_tasks.connect_sync")
    def test_system_logs_carry_trace_id_matching_task_id(
        self,
        mock_connect: MagicMock,
        mock_progress_cls: MagicMock,
        mock_sys_log_cls: MagicMock,
        _cache_cls: MagicMock,
        _repo_cls: MagicMock,
        _mb_cls: MagicMock,
    ) -> None:
        mock_connect.side_effect = _fake_connect_factory()
        mock_progress_repo = MagicMock()
        mock_progress_cls.return_value = mock_progress_repo
        mock_sys_log = MagicMock()
        mock_sys_log_cls.return_value = mock_sys_log

        from backend.tasks.library_enrichment_tasks import library_enrichment_task
        library_enrichment_task.call_local()

        first_progress = _progress_calls(mock_progress_repo)[0]
        task_id = first_progress.task_id

        log_entries = _system_log_calls(mock_sys_log)
        assert log_entries
        assert all(entry.trace_id == task_id for entry in log_entries)
