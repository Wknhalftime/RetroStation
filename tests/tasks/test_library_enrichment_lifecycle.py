"""AUD-025 characterisation tests for `library_enrichment_task`'s
RUNNING/COMPLETED/FAILED lifecycle envelope, now implemented via
`backend.tasks._task_run.task_run`. These pin the exact row sequence with
syrupy; the .ambr is byte-identical to the pre-extraction baseline (see git
history of this file — the gate-1 commit captured it against the old,
inline implementation).

Follows the MagicMock-patching setup already used by
tests/tasks/test_library_enrichment_progress.py (connect_sync /
RepositoryFactory / MusicBrainzApiClient mocked; no Postgres, no network).
The progress/SystemLog connection and repositories now live in
`_task_run`, so those three are patched there instead of on
`library_enrichment_tasks`; the ordered recorders in `_lifecycle_recording`
capture the full interleaved row sequence for snapshotting.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from syrupy.assertion import SnapshotAssertion

from tests.fakes.system_logs import FakeSystemLogRepository
from tests.tasks._lifecycle_recording import (
    FlakyProgressRepo,
    LifecycleEvent,
    OrderedProgressRepo,
    OrderedSystemLogRepo,
    normalize_events,
)


def _fake_connect_factory(
    release_rows: list[dict[str, str]] | None = None,
    recording_rows: list[dict[str, str]] | None = None,
) -> Any:
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


class _Patched:
    """Bundles the standard patch decorators shared by every test below.

    Innermost first: mock args land in this order. The progress/SystemLog
    connection and repositories are constructed inside
    `backend.tasks._task_run` (the shared envelope), not on
    `library_enrichment_tasks` itself — patched there accordingly.
    `_task_run`'s `connect_sync` always returns a bare, pre-configured mock
    connection (its real identity is irrelevant: `PgTaskProgressRepository`
    / `PgSystemLogRepository` are themselves replaced by the ordered
    recorders below, so nothing ever reads through it).
    """

    @staticmethod
    def apply(func: Any) -> Any:
        func = patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())(func)
        func = patch("backend.tasks.library_enrichment_tasks.connect_sync")(func)
        func = patch("backend.tasks.library_enrichment_tasks.enrich_by_recording", return_value=1)(
            func
        )
        func = patch("backend.tasks.library_enrichment_tasks.enrich_by_release", return_value=1)(
            func
        )
        func = patch("backend.tasks._task_run.PgTaskProgressRepository")(func)
        func = patch("backend.tasks._task_run.PgSystemLogRepository")(func)
        func = patch("backend.tasks.library_enrichment_tasks.PgMusicBrainzCacheRepository")(func)
        func = patch("backend.tasks.library_enrichment_tasks.RepositoryFactory")(func)
        func = patch("backend.tasks.library_enrichment_tasks.MusicBrainzApiClient")(func)
        return func


@_Patched.apply
def test_success_with_periodic_rows_full_envelope_sequence(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    _enrich_recording: MagicMock,
    _enrich_release: MagicMock,
    mock_progress_cls: MagicMock,
    mock_sys_log_cls: MagicMock,
    _cache_cls: MagicMock,
    _repo_cls: MagicMock,
    _mb_cls: MagicMock,
    snapshot: SnapshotAssertion,
) -> None:
    """One release, one recording, zero batches: exercises the initial
    RUNNING row, two periodic per-item RUNNING rows, COMPLETED, and the
    started/completed SystemLog pair — the full happy-path sequence.

    `mb_enrichment_task` is patched to a no-op so this test observes only
    library_enrichment's own envelope.
    """
    events: list[LifecycleEvent] = []
    mock_connect.side_effect = _fake_connect_factory(
        release_rows=[{"release_mbid": "r1"}],
        recording_rows=[{"recording_mbid": "rec1"}],
    )
    mock_progress_cls.return_value = OrderedProgressRepo(events)
    mock_sys_log_cls.return_value = OrderedSystemLogRepo(events)

    with patch("backend.tasks.mb_enrichment_tasks.mb_enrichment_task", MagicMock()):
        from backend.tasks.library_enrichment_tasks import library_enrichment_task

        result = library_enrichment_task.call_local()

    assert result == {"enriched": 2, "failed": 0}
    assert normalize_events(events) == snapshot


@_Patched.apply
def test_mid_run_exception_reports_failed_with_partial_progress(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    _enrich_recording: MagicMock,
    mock_enrich_release: MagicMock,
    mock_progress_cls: MagicMock,
    mock_sys_log_cls: MagicMock,
    _cache_cls: MagicMock,
    _repo_cls: MagicMock,
    _mb_cls: MagicMock,
    snapshot: SnapshotAssertion,
) -> None:
    """The 2nd release raises a non-retriable AttributeError (a logic bug,
    not one of `_PER_ITEM_RETRIABLE_ERRORS`). It is NOT caught by the
    per-item try/except, so it propagates to the task boundary. The 1st
    release's periodic report already ran, so FAILED must report
    processed=1 (not 0), and the failed SystemLog must carry a traceback.
    """
    events: list[LifecycleEvent] = []
    mock_enrich_release.side_effect = [1, AttributeError("boom mid-run")]
    mock_connect.side_effect = _fake_connect_factory(
        release_rows=[{"release_mbid": "r1"}, {"release_mbid": "r2"}],
        recording_rows=[],
    )
    mock_progress_cls.return_value = OrderedProgressRepo(events)
    mock_sys_log_cls.return_value = OrderedSystemLogRepo(events)

    with patch("backend.tasks.mb_enrichment_tasks.mb_enrichment_task", MagicMock()):
        from backend.tasks.library_enrichment_tasks import library_enrichment_task

        with pytest.raises(AttributeError, match="boom mid-run"):
            library_enrichment_task.call_local()

    normalized = normalize_events(events)
    failed_rows = [e for e in normalized if e["kind"] == "progress" and e["status"] == "failed"]
    assert len(failed_rows) == 1
    assert failed_rows[0]["progress_data"]["processed"] == 1
    assert failed_rows[0]["progress_data"]["total"] == 2

    failed_logs = [e for e in normalized if e["kind"] == "log" and e["level"] == "ERROR"]
    assert len(failed_logs) == 1
    assert failed_logs[0]["details"]["traceback"] == "<TRACEBACK>"

    assert normalized == snapshot


@_Patched.apply
def test_telemetry_write_failure_on_periodic_upsert_still_reraises_and_records_failed(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    _enrich_recording: MagicMock,
    _enrich_release: MagicMock,
    mock_progress_cls: MagicMock,
    mock_sys_log_cls: MagicMock,
    _cache_cls: MagicMock,
    _repo_cls: MagicMock,
    _mb_cls: MagicMock,
    snapshot: SnapshotAssertion,
) -> None:
    """`progress_repo.upsert` itself raises on the first periodic per-release
    report (call #2: #1 is the initial RUNNING upsert). Today, nothing
    wraps that call, so the telemetry failure IS the primary exception the
    task re-raises. The FAILED-path upsert (call #3) is a fresh call, so it
    still records `processed=1` under `contextlib.suppress`.
    """
    events: list[LifecycleEvent] = []
    mock_connect.side_effect = _fake_connect_factory(release_rows=[{"release_mbid": "r1"}])
    flaky_progress_repo = FlakyProgressRepo(events, fail_on_call=2)
    mock_progress_cls.return_value = flaky_progress_repo
    mock_sys_log_cls.return_value = OrderedSystemLogRepo(events)

    with patch("backend.tasks.mb_enrichment_tasks.mb_enrichment_task", MagicMock()):
        from backend.tasks.library_enrichment_tasks import library_enrichment_task

        with pytest.raises(RuntimeError, match="simulated telemetry write failure"):
            library_enrichment_task.call_local()

    normalized = normalize_events(events)
    failed_rows = [e for e in normalized if e["kind"] == "progress" and e["status"] == "failed"]
    assert len(failed_rows) == 1
    assert failed_rows[0]["progress_data"]["processed"] == 1
    assert failed_rows[0]["progress_data"]["error"] == "simulated telemetry write failure"

    assert normalized == snapshot


@_Patched.apply
def test_mb_enrichment_task_called_after_envelope_closes_its_failure_is_not_recorded(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    _enrich_recording: MagicMock,
    _enrich_release: MagicMock,
    mock_progress_cls: MagicMock,
    mock_sys_log_cls: MagicMock,
    _cache_cls: MagicMock,
    _repo_cls: MagicMock,
    _mb_cls: MagicMock,
    snapshot: SnapshotAssertion,
) -> None:
    """`mb_enrichment_task()` is called AFTER library_enrichment's own
    try/except/finally has exited (progress_conn already closed). Since
    AUD-R012 (1), that call is guarded by `enqueue_or_log`, but only
    for the real SqliteHuey enqueue-failure type (`sqlite3.Error`) — see
    `test_mb_enrichment_enqueue_failure_is_logged_and_does_not_propagate`
    for that case. An `httpx.HTTPError` (a pipeline-stage failure, not an
    enqueue failure) is NOT one of those, so it still propagates out of
    `library_enrichment_task` uncaught, WITHOUT library_enrichment ever
    writing a FAILED row for its own (successfully completed) envelope.
    """
    events: list[LifecycleEvent] = []
    mock_connect.side_effect = _fake_connect_factory()  # zero pending work
    mock_progress_cls.return_value = OrderedProgressRepo(events)
    mock_sys_log_cls.return_value = OrderedSystemLogRepo(events)

    with patch(
        "backend.tasks.mb_enrichment_tasks.mb_enrichment_task",
        MagicMock(side_effect=httpx.HTTPError("mb enrichment pipeline stage failed")),
    ):
        from backend.tasks.library_enrichment_tasks import library_enrichment_task

        with pytest.raises(httpx.HTTPError, match="mb enrichment pipeline stage failed"):
            library_enrichment_task.call_local()

    normalized = normalize_events(events)
    statuses = [e["status"] for e in normalized if e["kind"] == "progress"]
    assert statuses == ["running", "completed"], (
        "library_enrichment's own envelope must show a clean COMPLETED — "
        "the downstream mb_enrichment_task failure is not its FAILED"
    )
    assert normalized == snapshot


@_Patched.apply
def test_mb_enrichment_enqueue_failure_is_logged_and_does_not_propagate(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    _enrich_recording: MagicMock,
    _enrich_release: MagicMock,
    mock_progress_cls: MagicMock,
    mock_sys_log_cls: MagicMock,
    _cache_cls: MagicMock,
    _repo_cls: MagicMock,
    _mb_cls: MagicMock,
) -> None:
    """AUD-R012 (1): a real SqliteHuey enqueue failure (`sqlite3.Error`)
    calling `mb_enrichment_task()` is the caller's own problem to report. It
    must be logged as an ERROR SystemLog on library_enrichment's own
    task_id, and must NOT propagate out of `library_enrichment_task` —
    unlike the non-enqueue `httpx.HTTPError` case above, which still does.
    """
    events: list[LifecycleEvent] = []
    mock_connect.side_effect = _fake_connect_factory()  # zero pending work
    mock_progress_cls.return_value = OrderedProgressRepo(events)
    mock_sys_log_cls.return_value = OrderedSystemLogRepo(events)

    enqueue_failure_log = FakeSystemLogRepository()

    with (
        patch(
            "backend.tasks.mb_enrichment_tasks.mb_enrichment_task",
            MagicMock(side_effect=sqlite3.OperationalError("database is locked")),
        ),
        patch(
            "backend.tasks.library_enrichment_tasks.PgSystemLogRepository",
            return_value=enqueue_failure_log,
        ),
    ):
        from backend.tasks.library_enrichment_tasks import library_enrichment_task

        # Must NOT raise: the caller owns the handoff and keeps COMPLETED.
        result = library_enrichment_task.call_local()

    assert result == {"enriched": 0, "failed": 0}
    normalized = normalize_events(events)
    statuses = [e["status"] for e in normalized if e["kind"] == "progress"]
    assert statuses == ["running", "completed"], (
        "library_enrichment's own envelope must show a clean COMPLETED even "
        "though the downstream enqueue failed"
    )

    enqueue_failed_logs = [
        log for log in enqueue_failure_log.all if log.message == "mb_enrichment_task_enqueue_failed"
    ]
    assert len(enqueue_failed_logs) == 1
    assert enqueue_failed_logs[0].details is not None
    assert enqueue_failed_logs[0].details["error"] == "database is locked"


# ---------------------------------------------------------------------------
# AUD-025 gate-2 mutation-kill test — a narrow, plain-assertion test (no
# snapshot) pinning the pre-count-failure fallback defaults a cosmic-ray
# baseline run found under-covered (mirrors the mb_enrichment_tasks.py
# `total = 0` gap).
# ---------------------------------------------------------------------------


@_Patched.apply
def test_exception_before_total_is_computed_reports_failed_with_zero_processed_and_total(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    _enrich_recording: MagicMock,
    _enrich_release: MagicMock,
    mock_progress_cls: MagicMock,
    mock_sys_log_cls: MagicMock,
    _cache_cls: MagicMock,
    mock_repo_cls: MagicMock,
    _mb_cls: MagicMock,
) -> None:
    """An exception fetching the first pending-work queue (before `total`
    is computed from chunk/release/recording counts) must report FAILED
    with `processed=0, total=0` — the fallback defaults on what becomes
    `config.read_progress()` after the `task_run` extraction. Kills a
    cosmic-ray survivor that mutated the `total = 0` initializer.
    """
    events: list[LifecycleEvent] = []
    mock_connect.side_effect = _fake_connect_factory()
    mock_repo_cls.return_value.library_files.get_pending_enrichment_with_release.side_effect = (
        RuntimeError("pre-count boom")
    )
    mock_progress_cls.return_value = OrderedProgressRepo(events)
    mock_sys_log_cls.return_value = OrderedSystemLogRepo(events)

    with patch("backend.tasks.mb_enrichment_tasks.mb_enrichment_task", MagicMock()):
        from backend.tasks.library_enrichment_tasks import library_enrichment_task

        with pytest.raises(RuntimeError, match="pre-count boom"):
            library_enrichment_task.call_local()

    normalized = normalize_events(events)
    failed_rows = [e for e in normalized if e["kind"] == "progress" and e["status"] == "failed"]
    assert len(failed_rows) == 1
    assert failed_rows[0]["progress_data"]["processed"] == 0
    assert failed_rows[0]["progress_data"]["total"] == 0
