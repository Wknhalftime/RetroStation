"""AUD-025 characterisation tests for `mb_enrichment_task`'s
RUNNING/COMPLETED/FAILED lifecycle envelope, now implemented via
`backend.tasks._task_run.task_run`. These pin the exact row sequence with
syrupy; the .ambr is byte-identical to the pre-extraction baseline (see git
history of this file — the gate-1 commit captured it against the old,
inline implementation).

Follows the MagicMock-patching setup already used by
tests/tasks/test_mb_enrichment_progress.py (connect_sync / RepositoryFactory
/ MusicBrainzApiClient mocked; no Postgres, no network). The
progress/SystemLog connection and repositories now live in `_task_run`, so
those three are patched there instead of on `mb_enrichment_tasks`; the
ordered recorders in `_lifecycle_recording` capture the full interleaved
row sequence for snapshotting.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from syrupy.assertion import SnapshotAssertion

from tests.tasks._lifecycle_recording import (
    FlakyProgressRepo,
    LifecycleEvent,
    OrderedProgressRepo,
    OrderedSystemLogRepo,
    normalize_events,
)


@pytest.fixture(autouse=True)
def _no_recheck_hand_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """mb_enrichment_task ends by queuing rematch_undecided_task("changed") (spec 2026-10-05
    §4.2). Stub it so these tests never put a re-check on the real Huey queue."""
    monkeypatch.setattr("backend.tasks.matching_recheck_tasks.rematch_undecided_task", MagicMock())
    # D15 (AUD-R026): the MB pass first queues link_local_artists_task(); stub it too.
    monkeypatch.setattr("backend.tasks.artist_linking_tasks.link_local_artists_task", MagicMock())


def _mk_conn() -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = MagicMock()
    cur.rowcount = 0
    conn.execute.return_value = cur
    return conn


def _fake_connect(_url: str, *, autocommit: bool = False) -> MagicMock:
    return _mk_conn()


def _stub_repo_factory(artists: list[Any], works: list[Any], recordings: list[Any]) -> Any:
    def factory(_conn: Any) -> MagicMock:
        repos = MagicMock()
        repos.artists.list_unenhanced.return_value = artists
        repos.works.list_needing_enhancement.return_value = works
        repos.recordings.list_needing_enhancement.return_value = recordings
        return repos

    return factory


def _make_entity(mbid: str, name_field: str = "name") -> MagicMock:
    """Bare (mbid=None) entity: quarantined immediately, no MB call."""
    ent = MagicMock()
    ent.id = mbid
    ent.mbid = None
    ent.disambiguation = None
    ent.sort_name = f"Name-{mbid}"
    setattr(ent, name_field, f"Name-{mbid}")
    ent.duration_ms = None
    return ent


class _Patched:
    """Bundles the standard patch decorators shared by every test below.

    The progress/SystemLog connection and repositories are constructed
    inside `backend.tasks._task_run` (the shared envelope), not on
    `mb_enrichment_tasks` itself — patched there accordingly. `_task_run`'s
    `connect_sync` always returns a bare, pre-configured mock connection
    (its real identity is irrelevant: `PgTaskProgressRepository` /
    `PgSystemLogRepository` are themselves replaced by the ordered
    recorders below, so nothing ever reads through it) — tests that care
    about the call itself (e.g. the `autocommit=True` contract) inspect
    `task_run_connect` directly.
    """

    @staticmethod
    def apply(func: Any) -> Any:
        # Innermost first: mock args land in this same order.
        func = patch("backend.tasks._task_run.connect_sync", return_value=MagicMock())(func)
        func = patch("backend.tasks.mb_enrichment_tasks.connect_sync")(func)
        func = patch("backend.tasks._task_run.PgTaskProgressRepository")(func)
        func = patch("backend.tasks._task_run.PgSystemLogRepository")(func)
        func = patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory")(func)
        func = patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository")(func)
        func = patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient")(func)
        return func


@_Patched.apply
def test_success_with_periodic_rows_full_envelope_sequence(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    mock_progress_cls: MagicMock,
    mock_sys_log_cls: MagicMock,
    mock_factory_cls: MagicMock,
    _cache_cls: MagicMock,
    mock_mb_cls: MagicMock,
    snapshot: SnapshotAssertion,
) -> None:
    """Two bare artists (immediate per-item quarantine), zero works/recordings.

    Exercises: initial RUNNING, two periodic `_advance_progress` RUNNING
    rows (periodic progress rows), COMPLETED, and the started/completed
    SystemLog pair — the full happy-path envelope sequence.
    """
    events: list[LifecycleEvent] = []
    artists = [_make_entity("a0"), _make_entity("a1")]

    mock_connect.side_effect = _fake_connect
    mock_factory_cls.side_effect = _stub_repo_factory(artists, [], [])
    mock_progress_cls.return_value = OrderedProgressRepo(events)
    mock_sys_log_cls.return_value = OrderedSystemLogRepo(events)
    mock_mb_cls.return_value.lookup_artist.return_value = None
    mock_mb_cls.return_value.lookup_recording.return_value = None
    mock_mb_cls.return_value.live_fetches = 0
    mock_mb_cls.return_value.cache_hits = 0
    mock_mb_cls.return_value.__enter__ = lambda self: self
    mock_mb_cls.return_value.__exit__ = lambda self, *exc: False

    from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

    result = mb_enrichment_task.call_local()

    assert result == {
        "artists_done": 0,
        "artists_failed": 2,
        "works_done": 0,
        "works_failed": 0,
        "recordings_done": 0,
        "recordings_failed": 0,
    }
    assert normalize_events(events) == snapshot


@_Patched.apply
def test_mid_run_exception_reports_failed_with_partial_progress(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    mock_progress_cls: MagicMock,
    mock_sys_log_cls: MagicMock,
    mock_factory_cls: MagicMock,
    _cache_cls: MagicMock,
    mock_mb_cls: MagicMock,
    snapshot: SnapshotAssertion,
) -> None:
    """The 2nd artist's quarantine write raises an unexpected (non-retriable)
    TypeError. It is NOT in `_PER_ITEM_RETRIABLE_ERRORS`, so it propagates
    out of `_run_artist_phase` to the task's outer boundary. The 1st
    artist's `_advance_progress` already ran, so FAILED must report
    processed=1 (not 0), and the failed SystemLog must carry a traceback.
    """
    events: list[LifecycleEvent] = []
    artists = [_make_entity("a0"), _make_entity("a1")]

    mock_connect.side_effect = _fake_connect
    repos_factory = _stub_repo_factory(artists, [], [])

    def factory_with_flaky_second_call(conn: Any) -> MagicMock:
        repos = repos_factory(conn)
        repos.artists.mark_enhancement_failed.side_effect = [None, TypeError("boom mid-run")]
        return repos

    mock_factory_cls.side_effect = factory_with_flaky_second_call
    mock_progress_cls.return_value = OrderedProgressRepo(events)
    mock_sys_log_cls.return_value = OrderedSystemLogRepo(events)
    mock_mb_cls.return_value.lookup_artist.return_value = None
    mock_mb_cls.return_value.__enter__ = lambda self: self
    mock_mb_cls.return_value.__exit__ = lambda self, *exc: False

    from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

    with pytest.raises(TypeError, match="boom mid-run"):
        mb_enrichment_task.call_local()

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
    mock_progress_cls: MagicMock,
    mock_sys_log_cls: MagicMock,
    mock_factory_cls: MagicMock,
    _cache_cls: MagicMock,
    mock_mb_cls: MagicMock,
    snapshot: SnapshotAssertion,
) -> None:
    """`progress_repo.upsert` itself raises on the first periodic
    `_advance_progress` call (call #2: #1 is the initial RUNNING upsert).
    Today, nothing wraps that call, so the telemetry failure IS the primary
    exception the task re-raises. The FAILED-path upsert (call #3) is a
    fresh call, so it still records `processed=1` (incremented before the
    raising upsert call happened) under `contextlib.suppress`.
    """
    events: list[LifecycleEvent] = []
    artists = [_make_entity("a0")]

    mock_connect.side_effect = _fake_connect
    mock_factory_cls.side_effect = _stub_repo_factory(artists, [], [])
    flaky_progress_repo = FlakyProgressRepo(events, fail_on_call=2)
    mock_progress_cls.return_value = flaky_progress_repo
    mock_sys_log_cls.return_value = OrderedSystemLogRepo(events)
    mock_mb_cls.return_value.lookup_artist.return_value = None
    mock_mb_cls.return_value.__enter__ = lambda self: self
    mock_mb_cls.return_value.__exit__ = lambda self, *exc: False

    from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

    with pytest.raises(RuntimeError, match="simulated telemetry write failure"):
        mb_enrichment_task.call_local()

    normalized = normalize_events(events)
    failed_rows = [e for e in normalized if e["kind"] == "progress" and e["status"] == "failed"]
    assert len(failed_rows) == 1
    assert failed_rows[0]["progress_data"]["processed"] == 1
    assert failed_rows[0]["progress_data"]["error"] == "simulated telemetry write failure"

    assert normalized == snapshot


# ---------------------------------------------------------------------------
# AUD-025 gate-2 mutation-kill tests — narrow, plain-assertion tests (no
# snapshot) pinning two envelope-contract details a cosmic-ray baseline run
# found under-covered: the pre-count-failure fallback defaults, and the
# progress connection's `autocommit=True` requirement.
# ---------------------------------------------------------------------------


@_Patched.apply
def test_exception_before_precount_reports_failed_with_zero_processed_and_total(
    _task_run_connect: MagicMock,
    mock_connect: MagicMock,
    mock_progress_cls: MagicMock,
    mock_sys_log_cls: MagicMock,
    mock_factory_cls: MagicMock,
    _cache_cls: MagicMock,
    _mb_cls: MagicMock,
) -> None:
    """An exception during pre-count (before `ctx` exists and before `total`
    is computed) must report FAILED with `processed=0, total=0` — the
    fallback defaults on `config.read_progress()`. Kills a cosmic-ray
    survivor that mutated both the `total = 0` initializer and the
    `else 0` fallback in the FAILED payload.

    The progress connection now lives in `_task_run`, so the counting
    connection (`mb_enrichment_tasks.connect_sync`'s only remaining caller
    besides the per-phase connections) is the first and only call that
    needs to fail here.
    """
    events: list[LifecycleEvent] = []
    mock_connect.side_effect = RuntimeError("pre-count boom")
    mock_factory_cls.side_effect = _stub_repo_factory([], [], [])
    mock_progress_cls.return_value = OrderedProgressRepo(events)
    mock_sys_log_cls.return_value = OrderedSystemLogRepo(events)

    from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

    with pytest.raises(RuntimeError, match="pre-count boom"):
        mb_enrichment_task.call_local()

    normalized = normalize_events(events)
    failed_rows = [e for e in normalized if e["kind"] == "progress" and e["status"] == "failed"]
    assert len(failed_rows) == 1
    assert failed_rows[0]["progress_data"]["processed"] == 0
    assert failed_rows[0]["progress_data"]["total"] == 0


@_Patched.apply
def test_progress_connection_is_opened_autocommit(
    task_run_connect: MagicMock,
    mock_connect: MagicMock,
    mock_progress_cls: MagicMock,
    mock_sys_log_cls: MagicMock,
    mock_factory_cls: MagicMock,
    _cache_cls: MagicMock,
    mock_mb_cls: MagicMock,
) -> None:
    """`task_run`'s dedicated progress/SystemLog connection must be opened
    with `autocommit=True`. Losing this silently re-introduces the
    WS-empty-during-mb_enrichment bug (see `_task_run.task_run`'s
    docstring). Kills a cosmic-ray survivor that flipped this to
    `autocommit=False`.
    """
    events: list[LifecycleEvent] = []
    mock_connect.side_effect = _fake_connect
    mock_factory_cls.side_effect = _stub_repo_factory([], [], [])
    mock_progress_cls.return_value = OrderedProgressRepo(events)
    mock_sys_log_cls.return_value = OrderedSystemLogRepo(events)
    mock_mb_cls.return_value.__enter__ = lambda self: self
    mock_mb_cls.return_value.__exit__ = lambda self, *exc: False

    from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

    mb_enrichment_task.call_local()

    task_run_connect.assert_called_once()
    assert task_run_connect.call_args.kwargs.get("autocommit") is True
