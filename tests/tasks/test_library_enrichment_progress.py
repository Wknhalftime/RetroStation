"""Progress-emission tests for library_enrichment_task."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
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
        # Use a realistic transient-failure type that the task is designed
        # to catch (httpx.HTTPError). A bare RuntimeError now correctly
        # propagates per the narrowed exception handler — only expected
        # MB/DB/parse failures are treated as per-item retriable.
        mock_enrich_release.side_effect = [httpx.HTTPError("boom"), 1]
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
        failed_log = next(e for e in log_entries if e.message == "enrichment_failed")
        assert "db down" in failed_log.details["traceback"]

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


def _pending_file(n: int) -> Any:
    from uuid import uuid4

    from backend.domain.enums import EnrichmentStatus
    from backend.domain.library import AudioMetadata, LibraryFile

    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{n:05d}.flac",
        file_hash=f"hash-{n}",
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        audio=AudioMetadata(release_mbid=f"rel-{n // 10}", recording_mbid=f"rec-{n}"),
    )


class TestLibraryEnrichmentBatchedPass:
    @patch("backend.tasks.mb_enrichment_tasks.mb_enrichment_task", MagicMock())
    @patch("backend.tasks.library_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.library_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks.library_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_enrichment_tasks.enrich_by_recording_batch")
    @patch("backend.tasks.library_enrichment_tasks.enrich_by_release", return_value=1)
    @patch("backend.tasks.library_enrichment_tasks.enrich_by_recording", return_value=1)
    @patch("backend.tasks.library_enrichment_tasks.connect_sync")
    def test_batches_of_100_files_each_committed_and_counted(
        self,
        mock_connect: MagicMock,
        _enrich_recording: MagicMock,
        _enrich_release: MagicMock,
        mock_batch: MagicMock,
        mock_progress_cls: MagicMock,
        _sys_log_cls: MagicMock,
        _cache_cls: MagicMock,
        mock_repo_cls: MagicMock,
        _mb_cls: MagicMock,
    ) -> None:
        from backend.services.library_enrichment_service import BatchEnrichment

        pending = [_pending_file(n) for n in range(250)]
        files_repo = mock_repo_cls.return_value.library_files
        files_repo.get_pending_enrichment_with_release.return_value = pending
        mock_batch.side_effect = lambda files, *_a, **_k: BatchEnrichment(
            enriched=len(files), unresolved=(),
        )
        mock_connect.side_effect = _fake_connect_factory(release_rows=[{"release_mbid": "r1"}])
        mock_progress_repo = MagicMock()
        mock_progress_cls.return_value = mock_progress_repo

        from backend.tasks.library_enrichment_tasks import library_enrichment_task
        outcome = library_enrichment_task.call_local()

        chunks = [call.args[0] for call in mock_batch.call_args_list]
        assert [len(c) for c in chunks] == [100, 100, 50]
        assert chunks[0][0].file_path == "/music/00000.flac"
        # 250 files linked by the batches, one more by the per-release fallback.
        assert outcome == {"enriched": 251, "failed": 0}
        running = [c for c in _progress_calls(mock_progress_repo) if c.status == TaskStatus.RUNNING]
        assert running[0].progress_data["total"] == 4  # 3 batches + 1 release
        assert [c.progress_data["processed"] for c in running] == [0, 1, 2, 3, 4]
        assert running[1].progress_data["current_item"] == "batch:1/3"

    @patch("backend.tasks.mb_enrichment_tasks.mb_enrichment_task", MagicMock())
    @patch("backend.tasks.library_enrichment_tasks.MusicBrainzApiClient")
    @patch("backend.tasks.library_enrichment_tasks.RepositoryFactory")
    @patch("backend.tasks.library_enrichment_tasks.PgMusicBrainzCacheRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgSystemLogRepository")
    @patch("backend.tasks.library_enrichment_tasks.PgTaskProgressRepository")
    @patch("backend.tasks.library_enrichment_tasks.enrich_by_recording_batch")
    @patch("backend.tasks.library_enrichment_tasks.enrich_by_release", return_value=1)
    @patch("backend.tasks.library_enrichment_tasks.enrich_by_recording", return_value=1)
    @patch("backend.tasks.library_enrichment_tasks.connect_sync")
    def test_batch_failure_leaves_files_to_the_per_release_fallback(
        self,
        mock_connect: MagicMock,
        _enrich_recording: MagicMock,
        mock_enrich_release: MagicMock,
        mock_batch: MagicMock,
        mock_progress_cls: MagicMock,
        _sys_log_cls: MagicMock,
        _cache_cls: MagicMock,
        mock_repo_cls: MagicMock,
        _mb_cls: MagicMock,
    ) -> None:
        order: list[str] = []
        mock_batch.side_effect = lambda *_a, **_k: (order.append("batch"), _raise())[1]
        mock_enrich_release.side_effect = lambda *_a, **_k: (order.append("release"), 1)[1]
        files_repo = mock_repo_cls.return_value.library_files
        files_repo.get_pending_enrichment_with_release.return_value = [_pending_file(1)]
        mock_connect.side_effect = _fake_connect_factory(release_rows=[{"release_mbid": "rel-0"}])
        mock_progress_cls.return_value = MagicMock()

        from backend.tasks.library_enrichment_tasks import library_enrichment_task
        outcome = library_enrichment_task.call_local()

        # The batch raised a transient error: it counts as one failure, its
        # transaction is rolled back, and the release loop still runs after it.
        assert order == ["batch", "release"]
        assert outcome == {"enriched": 1, "failed": 1}


def _raise() -> None:
    raise httpx.HTTPError("boom")
