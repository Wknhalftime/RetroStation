"""Acceptance tests for the AUD-R014 / AUD-R020 hand-off fixes (findings AUD-059, AUD-061..063).

Requirements, each traced to the rulings in audit/rulings.jsonl:

- H1 (AUD-R020): ingestion_task hands off to artist_matching_task itself, through
  enqueue_or_log, independently of the embedding_task hand-off. A failed embedding
  enqueue never stops artist matching, and vice versa.
- H2 (AUD-R020): embedding_task no longer enqueues anything. Matching does not wait on
  embedding, because nothing reads the vectors.
- H3 (AUD-R014, AUD-R012 (1)): artist_matching_task hands off to identity_matching_task
  through enqueue_or_log. A storage failure (sqlite3.Error) is logged on the matching
  run's own task_id and does not fail the run. Any other exception propagates.
- H4 (AUD-R014 + Lance 2026-10-05): library_watcher_poll hands off to
  library_scan_files_task through enqueue_or_log. On a storage failure it logs and
  releases the folders it staged for that scan, so the next 4-minute poll retries
  them instead of waiting out the 1-hour staging TTL.
- H5: enqueue_or_log takes an optional `on_failure` callback, run only after a storage
  failure, and even when the failure SystemLog itself could not be written.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from backend.domain.enums import LogCategory, LogLevel, TaskStatus
from backend.domain.system import SystemLog
from backend.services.ingestion_service import IngestionResult
from backend.tasks._enqueue_chain import enqueue_or_log
from tests.fakes.library_folders import FakeLibraryFolderRepository
from tests.fakes.system_logs import FakeSystemLogRepository
from tests.fakes.task_progress import FakeTaskProgressRepository

CSV_PAYLOAD = b"Station,Played,Artist,Title\r\nKAZR,2005-03-02 00:01:00,Artist_A,Title_A\r\n"


def _fake_connect_sync(_url: str, *, autocommit: bool = False) -> Any:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    return conn


def _ingest_result(playlist_id: str) -> IngestionResult:
    return IngestionResult(
        playlist_id=playlist_id,
        rows_processed=1,
        rows_skipped=0,
        artists_created=1,
        identities_created=1,
        events_created=1,
        broadcast_days_created=1,
    )


def _failed_logs(repo: FakeSystemLogRepository, task_name: str) -> list[SystemLog]:
    return [log for log in repo.all if log.message == f"{task_name}_enqueue_failed"]


# ---------------------------------------------------------------------------
# H1: ingestion_task hands off to embedding and artist matching independently
# ---------------------------------------------------------------------------


@patch("backend.tasks.ingestion_tasks.count_csv_rows", return_value=1)
@patch("backend.tasks.ingestion_tasks._run_ingest")
@patch("backend.tasks.ingestion_tasks.PgSystemLogRepository")
@patch("backend.tasks.ingestion_tasks.PgTaskProgressRepository")
@patch("backend.tasks.ingestion_tasks.connect_sync", side_effect=_fake_connect_sync)
class TestIngestionHandsOffToMatchingDirectly:
    def _run(
        self,
        run_ingest: MagicMock,
        progress_cls: MagicMock,
        sys_log_cls: MagicMock,
        *,
        embedding: MagicMock,
        matching: MagicMock,
    ) -> tuple[FakeTaskProgressRepository, FakeSystemLogRepository]:
        progress = FakeTaskProgressRepository()
        progress_cls.return_value = progress
        sys_log = FakeSystemLogRepository()
        sys_log_cls.return_value = sys_log
        run_ingest.return_value = _ingest_result("pl-h1")

        from backend.tasks.ingestion_tasks import ingestion_task

        with (
            patch("backend.tasks.embedding_tasks.embedding_task", embedding),
            patch("backend.tasks.artist_matching_tasks.artist_matching_task", matching),
        ):
            ingestion_task.call_local(CSV_PAYLOAD, "f.csv", str(uuid4()), "tid-h1")
        return progress, sys_log

    def test_success_enqueues_both_embedding_and_artist_matching(
        self,
        _connect: MagicMock,
        progress_cls: MagicMock,
        sys_log_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        embedding, matching = MagicMock(), MagicMock()
        progress, _ = self._run(
            run_ingest, progress_cls, sys_log_cls, embedding=embedding, matching=matching
        )

        embedding.assert_called_once_with("pl-h1")
        matching.assert_called_once_with("pl-h1")
        assert progress.received_upserts[-1].status == TaskStatus.COMPLETED

    def test_failed_embedding_enqueue_still_enqueues_artist_matching(
        self,
        _connect: MagicMock,
        progress_cls: MagicMock,
        sys_log_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        embedding = MagicMock(side_effect=sqlite3.OperationalError("database is locked"))
        matching = MagicMock()
        progress, sys_log = self._run(
            run_ingest, progress_cls, sys_log_cls, embedding=embedding, matching=matching
        )

        matching.assert_called_once_with("pl-h1")
        assert len(_failed_logs(sys_log, "embedding_task")) == 1
        assert _failed_logs(sys_log, "artist_matching_task") == []
        assert progress.received_upserts[-1].status == TaskStatus.COMPLETED

    def test_failed_artist_matching_enqueue_is_logged_on_the_ingestion_run(
        self,
        _connect: MagicMock,
        progress_cls: MagicMock,
        sys_log_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        embedding = MagicMock()
        matching = MagicMock(side_effect=sqlite3.OperationalError("disk I/O error"))
        progress, sys_log = self._run(
            run_ingest, progress_cls, sys_log_cls, embedding=embedding, matching=matching
        )

        embedding.assert_called_once_with("pl-h1")
        logs = _failed_logs(sys_log, "artist_matching_task")
        assert len(logs) == 1
        assert logs[0].trace_id == "tid-h1"
        assert logs[0].level == LogLevel.ERROR
        assert logs[0].category == LogCategory.INGESTION
        assert logs[0].details is not None
        assert logs[0].details["error"] == "disk I/O error"
        assert progress.received_upserts[-1].status == TaskStatus.COMPLETED

    def test_non_storage_artist_matching_enqueue_error_propagates_and_fails_the_run(
        self,
        _connect: MagicMock,
        progress_cls: MagicMock,
        sys_log_cls: MagicMock,
        run_ingest: MagicMock,
        _count: MagicMock,
    ) -> None:
        """AUD-R012 (1): only sqlite3.Error is a failed hand-off. Anything else is a
        defect: it propagates and the run is reported FAILED, as for embedding today."""
        progress = FakeTaskProgressRepository()
        progress_cls.return_value = progress
        sys_log_cls.return_value = FakeSystemLogRepository()
        run_ingest.return_value = _ingest_result("pl-h1")

        from backend.tasks.ingestion_tasks import ingestion_task

        with (
            patch("backend.tasks.embedding_tasks.embedding_task", MagicMock()),
            patch(
                "backend.tasks.artist_matching_tasks.artist_matching_task",
                MagicMock(side_effect=RuntimeError("logic bug")),
            ),
            pytest.raises(RuntimeError, match="logic bug"),
        ):
            ingestion_task.call_local(CSV_PAYLOAD, "f.csv", str(uuid4()), "tid-h1b")

        assert progress.received_upserts[-1].status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# H2: embedding_task enqueues nothing
# ---------------------------------------------------------------------------


class _EmptyEmbeddingRepo:
    def __init__(self, conn: object) -> None:
        self.conn = conn

    def get_unembedded_for_playlist(self, playlist_id: UUID) -> list[object]:
        return []


def test_embedding_task_does_not_enqueue_artist_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "backend.tasks.embedding_tasks.connect_sync",
        lambda *args, **kwargs: _fake_connect_sync(""),
    )
    monkeypatch.setattr(
        "backend.tasks.embedding_tasks.PgBroadcastArtistRepository", _EmptyEmbeddingRepo
    )
    monkeypatch.setattr(
        "backend.tasks.embedding_tasks.PgBroadcastTrackIdentityRepository", _EmptyEmbeddingRepo
    )
    matching = MagicMock()
    monkeypatch.setattr("backend.tasks.artist_matching_tasks.artist_matching_task", matching)
    # Watch the queue itself too, so the test does not depend on how a task is imported.
    from backend.tasks.huey_app import huey

    enqueue = MagicMock()
    monkeypatch.setattr(huey, "enqueue", enqueue)

    from backend.tasks.embedding_tasks import embedding_task

    embedding_task.call_local(str(uuid4()))

    matching.assert_not_called()
    enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# H3: artist_matching_task -> identity_matching_task goes through enqueue_or_log
# ---------------------------------------------------------------------------


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeRepo:
    def __init__(self, conn: object) -> None:
        self.conn = conn

    def get_all_for_playlist(self, playlist_id: object) -> list[object]:
        return []

    def reset_deferred_by_ids(self, artist_ids: list[object]) -> int:
        return 0

    def reset_deferred_by_artist_ids(self, artist_ids: list[object]) -> int:
        return 0


class _FakeMbClient:
    def __init__(self, cache_repo: object, *, ttl_days: int) -> None:
        self.cache_repo = cache_repo

    def __enter__(self) -> _FakeMbClient:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _wire_artist_matching(
    monkeypatch: pytest.MonkeyPatch, identity: MagicMock
) -> tuple[FakeSystemLogRepository, list[str]]:
    settings = SimpleNamespace(
        database_url="postgresql://unused/unused",
        mb_cache_ttl_days=1,
        strong_match_threshold=1,
        mb_score_gap=1,
        mb_auto_link_score=1,
        min_presentation_score=1,
        broadcast_name_max_len=1,
    )
    module = "backend.tasks.artist_matching_tasks"
    monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    monkeypatch.setattr(f"{module}.connect_sync", lambda *args, **kwargs: _FakeConn())
    for name in (
        "PgMusicBrainzCacheRepository",
        "PgBroadcastArtistRepository",
        "PgBroadcastTrackIdentityRepository",
        "PgArtistRepository",
        "PgMatchRepository",
        "PgMappingRuleRepository",
    ):
        monkeypatch.setattr(f"{module}.{name}", _FakeRepo)
    monkeypatch.setattr(f"{module}.MusicBrainzApiClient", _FakeMbClient)
    monkeypatch.setattr(f"{module}.match_artists_for_playlist", lambda **kwargs: None)
    sys_log = FakeSystemLogRepository()
    monkeypatch.setattr(f"{module}.PgSystemLogRepository", lambda conn: sys_log)
    monkeypatch.setattr("backend.tasks.identity_matching_tasks.identity_matching_task", identity)

    # The matching run's task_id is the one its task_failure_telemetry envelope yields.
    import backend.tasks.artist_matching_tasks as artist_matching_tasks

    real_envelope = artist_matching_tasks.task_failure_telemetry
    run_ids: list[str] = []

    @contextmanager
    def _recording_envelope(*args: Any, **kwargs: Any) -> Iterator[str]:
        with real_envelope(*args, **kwargs) as task_id:
            run_ids.append(task_id)
            yield task_id

    monkeypatch.setattr(f"{module}.task_failure_telemetry", _recording_envelope)
    return sys_log, run_ids


def test_artist_matching_enqueues_identity_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    playlist_id = str(uuid4())
    identity = MagicMock()
    sys_log, _run_ids = _wire_artist_matching(monkeypatch, identity)

    from backend.tasks.artist_matching_tasks import artist_matching_task

    artist_matching_task.call_local(playlist_id)

    identity.assert_called_once_with(playlist_id)
    assert sys_log.all == []


def test_failed_identity_enqueue_is_logged_on_the_matching_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playlist_id = str(uuid4())
    identity = MagicMock(side_effect=sqlite3.OperationalError("database is locked"))
    sys_log, run_ids = _wire_artist_matching(monkeypatch, identity)

    from backend.tasks.artist_matching_tasks import artist_matching_task

    # Must not raise: artist results are committed; the hand-off is the caller's to log.
    artist_matching_task.call_local(playlist_id)

    logs = _failed_logs(sys_log, "identity_matching_task")
    assert len(logs) == 1
    assert len(run_ids) == 1
    assert logs[0].trace_id == run_ids[0]
    assert logs[0].level == LogLevel.ERROR
    assert logs[0].category == LogCategory.MATCHING
    assert logs[0].details is not None
    assert logs[0].details["error"] == "database is locked"


def test_non_storage_identity_enqueue_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = MagicMock(side_effect=RuntimeError("not a storage fault"))
    _wire_artist_matching(monkeypatch, identity)

    from backend.tasks.artist_matching_tasks import artist_matching_task

    with pytest.raises(RuntimeError, match="not a storage fault"):
        artist_matching_task.call_local(str(uuid4()))


# ---------------------------------------------------------------------------
# H4: library_watcher_poll -> library_scan_files_task goes through enqueue_or_log
# ---------------------------------------------------------------------------

_FOLDER_ID = UUID(int=0xF0)


class _RecordingFolderRepository(FakeLibraryFolderRepository):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def clear_staged_hashes(self, task_id: str) -> None:
        self.events.append("release")
        super().clear_staged_hashes(task_id)


def _run_poll(
    scan: MagicMock, folders: _RecordingFolderRepository | None = None
) -> tuple[_RecordingFolderRepository, FakeSystemLogRepository]:
    """Run one poll; the folder fake records "release" and "commit" in order."""
    from tests.fakes.user_settings import FakeUserSettingRepository

    if folders is None:
        folders = _RecordingFolderRepository([])
    events = folders.events
    sys_log = FakeSystemLogRepository()
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.commit.side_effect = lambda: events.append("commit")
    with (
        patch("backend.tasks.library_watcher_tasks.connect_sync", return_value=conn),
        # Either way of building the log repository lands in the same fake.
        patch("backend.tasks.library_watcher_tasks.PgSystemLogRepository", return_value=sys_log),
        patch(
            "backend.tasks.library_watcher_tasks.diff_tree",
            return_value=(["/music/jazz"], [(_FOLDER_ID, "new-hash")]),
        ),
        patch("backend.tasks.library_watcher_tasks.library_scan_files_task", scan),
        patch("backend.tasks.library_watcher_tasks.RepositoryFactory") as factory,
    ):
        factory.return_value.user_settings = FakeUserSettingRepository(
            initial={"local_path_prefix": "/music"},
        )
        factory.return_value.library_folders = folders
        factory.return_value.system_logs = sys_log

        from backend.tasks.library_watcher_tasks import library_watcher_poll

        library_watcher_poll.call_local()
    return folders, sys_log


def test_watcher_poll_success_keeps_folders_staged_for_the_scan() -> None:
    scan = MagicMock()
    folders, sys_log = _run_poll(scan)

    scan.assert_called_once()
    paths, task_id = scan.call_args[0]
    assert paths == ["/music/jazz"]
    assert isinstance(task_id, str)
    assert task_id
    assert folders.get_folders_with_staged_hashes() == {_FOLDER_ID}
    assert sys_log.all == []


def test_watcher_poll_failed_enqueue_logs_and_releases_staged_folders() -> None:
    scan = MagicMock(side_effect=sqlite3.OperationalError("database is locked"))
    folders, sys_log = _run_poll(scan)

    _paths, task_id = scan.call_args[0]
    logs = _failed_logs(sys_log, "library_scan_files_task")
    assert len(logs) == 1
    assert logs[0].trace_id == task_id
    assert logs[0].level == LogLevel.ERROR
    assert logs[0].category == LogCategory.SCAN
    assert logs[0].details is not None
    assert logs[0].details["error"] == "database is locked"
    # Released, so the next 4-minute poll sees these folders as changed again.
    assert folders.get_folders_with_staged_hashes() == set()


def test_watcher_poll_commits_the_release() -> None:
    folders, _ = _run_poll(MagicMock(side_effect=sqlite3.OperationalError("database is locked")))

    assert "release" in folders.events
    assert "commit" in folders.events[folders.events.index("release") :]


def test_watcher_poll_non_storage_enqueue_error_propagates_and_keeps_folders_staged() -> None:
    """AUD-R012 (1): a non-storage error is a defect. It propagates, and the folders
    stay staged (released later by the 1-hour TTL), because on_failure is not run."""
    folders = _RecordingFolderRepository([])
    with pytest.raises(RuntimeError, match="logic bug"):
        _run_poll(MagicMock(side_effect=RuntimeError("logic bug")), folders)

    assert "release" not in folders.events
    assert folders.get_folders_with_staged_hashes() == {_FOLDER_ID}


# ---------------------------------------------------------------------------
# H5: enqueue_or_log's optional on_failure callback
# ---------------------------------------------------------------------------


class _UnwritableSystemLogRepository(FakeSystemLogRepository):
    def create(self, log: SystemLog) -> None:
        raise RuntimeError("telemetry down")


def _call(
    enqueue: MagicMock, on_failure: MagicMock, repo: FakeSystemLogRepository | None = None
) -> FakeSystemLogRepository:
    sys_log = repo if repo is not None else FakeSystemLogRepository()
    enqueue_or_log(
        enqueue,
        task_name="next_task",
        caller_task_id="caller-1",
        log_category=LogCategory.SYSTEM,
        sys_log_repo=sys_log,
        on_failure=on_failure,
    )
    return sys_log


def test_on_failure_not_called_when_enqueue_succeeds() -> None:
    on_failure = MagicMock()
    _call(MagicMock(), on_failure)
    on_failure.assert_not_called()


def test_on_failure_called_once_after_the_failure_is_logged() -> None:
    order: list[str] = []

    class _RecordingRepo(FakeSystemLogRepository):
        def create(self, log: SystemLog) -> None:
            order.append("logged")
            super().create(log)

    on_failure = MagicMock(side_effect=lambda: order.append("on_failure"))
    sys_log = _call(
        MagicMock(side_effect=sqlite3.OperationalError("locked")), on_failure, _RecordingRepo()
    )

    on_failure.assert_called_once_with()
    assert order == ["logged", "on_failure"]
    assert len(_failed_logs(sys_log, "next_task")) == 1


def test_on_failure_still_called_when_the_failure_log_cannot_be_written() -> None:
    on_failure = MagicMock()
    _call(
        MagicMock(side_effect=sqlite3.OperationalError("locked")),
        on_failure,
        _UnwritableSystemLogRepository(),
    )
    on_failure.assert_called_once_with()


def test_on_failure_not_called_for_a_non_storage_error() -> None:
    on_failure = MagicMock()
    with pytest.raises(RuntimeError, match="logic bug"):
        _call(MagicMock(side_effect=RuntimeError("logic bug")), on_failure)
    on_failure.assert_not_called()


def test_an_exception_from_on_failure_propagates() -> None:
    with pytest.raises(ValueError, match="cleanup failed"):
        _call(
            MagicMock(side_effect=sqlite3.OperationalError("locked")),
            MagicMock(side_effect=ValueError("cleanup failed")),
        )
