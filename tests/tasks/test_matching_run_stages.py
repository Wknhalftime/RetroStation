"""Acceptance tests: the matching stages report to their run's row (spec 2026-10-17).

``artist_matching_task(playlist_id, run_id)`` and ``identity_matching_task(playlist_id,
run_id)`` get their ports from ``backend.tasks._matching_run.run_ports`` (patched here). With
a run id, a stage:

- counts the pending artists + songs once at its start and refreshes the row with that count
  at once;
- passes the matching service a ``progress`` observer. Each ``tick()`` is one settled item;
  the row is refreshed with ``pending = start count - ticks so far`` at most once per 2 s of
  the ports' clock (the start refresh counts as one);
- when its start count failed (None), never refreshes;
- reads nothing on its own transactional connection (its connection here has no ``execute``);
- finishes its stage with ``pending`` = a fresh count, or ``start count - ticks`` when that
  count fails, or None when both failed:
  - the artist stage finishes only when it does not hand off: its hand-off was refused, it
    raised, or the hand-off raised a non-storage error (which still propagates);
  - the artist stage's hand-off is ``identity_matching_task(playlist_id, run_id)``;
  - the identity stage always finishes: failed when it raised;
- still writes its own FAILED row through ``task_failure_telemetry`` and re-raises.

With no run id a stage never touches the run ports, and the artist stage hands off
``identity_matching_task(playlist_id)`` exactly, as today.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from backend.domain.enums import TaskStatus, TaskType
from backend.services.matching_progress import NO_PROGRESS
from backend.tasks.artist_matching_tasks import artist_matching_task
from backend.tasks.identity_matching_tasks import identity_matching_task
from tests.fakes.matching_run import FakeMatchingRunRepository
from tests.fakes.pending_match_work import FakePendingMatchWork
from tests.fakes.system_logs import FakeSystemLogRepository
from tests.fakes.task_progress import FakeTaskProgressRepository

ARTIST_MODULE = "backend.tasks.artist_matching_tasks"
IDENTITY_MODULE = "backend.tasks.identity_matching_tasks"


class _StageConn:
    """The stage's transactional connection: no ``execute``, so any read on it fails."""

    def __enter__(self) -> _StageConn:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def commit(self) -> None:
        pass


class _Repo:
    def __init__(self, conn: object) -> None:
        self.conn = conn

    def get_all_for_playlist(self, playlist_id: object) -> list[object]:
        return []

    def reset_deferred_by_ids(self, ids: list[object]) -> int:
        return 0

    def reset_deferred_by_artist_ids(self, ids: list[object]) -> int:
        return 0


class _MbClient:
    def __init__(self, cache_repo: object, *, ttl_days: int) -> None:
        pass

    def __enter__(self) -> _MbClient:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _no_database(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("a unit test reached a database")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Rig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.runs = FakeMatchingRunRepository()
        self.pending = FakePendingMatchWork(10, 4)
        self.clock = _Clock()
        self.ports_opened = 0
        self.failure_progress = FakeTaskProgressRepository()
        self.sys_log = FakeSystemLogRepository()
        self.identity = MagicMock()
        # The service stand-in: tick at these clock times, then return or raise.
        self.tick_times: list[float] = []
        self.service_error: Exception | None = None
        settings = SimpleNamespace(
            database_url="postgresql://unused/unused",
            mb_cache_ttl_days=1,
            strong_match_threshold=1,
            mb_score_gap=1,
            mb_auto_link_score=1,
            min_presentation_score=1,
            broadcast_name_max_len=1,
        )
        for module in (ARTIST_MODULE, IDENTITY_MODULE):
            monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
            monkeypatch.setattr(f"{module}.connect_sync", lambda *a, **k: _StageConn())
            monkeypatch.setattr(f"{module}.MusicBrainzApiClient", _MbClient)
            for name in (
                "PgMusicBrainzCacheRepository",
                "PgBroadcastArtistRepository",
                "PgBroadcastTrackIdentityRepository",
                "PgArtistRepository",
                "PgMatchRepository",
                "PgMappingRuleRepository",
                "PgLibraryFileRepository",
                "PgSongMasterRepository",
                "PgRecordingRepository",
            ):
                monkeypatch.setattr(f"{module}.{name}", _Repo, raising=False)
        monkeypatch.setattr(f"{ARTIST_MODULE}.PgSystemLogRepository", lambda conn: self.sys_log)
        monkeypatch.setattr(f"{ARTIST_MODULE}.match_artists_for_playlist", self._service)
        monkeypatch.setattr(f"{IDENTITY_MODULE}.match_identities_for_playlist", self._service)
        monkeypatch.setattr(f"{IDENTITY_MODULE}.identity_matching_task", self.identity)
        monkeypatch.setattr("backend.tasks._matching_run.run_ports", self._run_ports)
        # A tripwire: these tests never reach a database.
        monkeypatch.setattr("backend.tasks._matching_run.connect_sync", _no_database, raising=False)
        # The identity stage recalculates song masters for the works it matched.
        self.work_ids: list[str] = []
        self.finished_before_recalculation: list[int] = []
        monkeypatch.setattr(
            "backend.services.master_selection_service.recalculate_song_masters",
            self._recalculate,
        )
        # task_failure_telemetry's FAILED row.
        monkeypatch.setattr(
            "backend.tasks._error_boundary.connect_sync", lambda *a, **k: _StageConn()
        )
        monkeypatch.setattr(
            "backend.tasks._error_boundary.PgTaskProgressRepository",
            lambda conn: self.failure_progress,
        )
        monkeypatch.setattr(
            "backend.tasks._error_boundary.PgSystemLogRepository", lambda conn: self.sys_log
        )

    @contextmanager
    def _run_ports(self, _database_url: str) -> Iterator[SimpleNamespace]:
        self.ports_opened += 1
        yield SimpleNamespace(runs=self.runs, pending=self.pending, clock=self.clock)

    def _service(self, **kwargs: Any) -> list[str]:
        progress = kwargs.get("progress", NO_PROGRESS)
        for at in self.tick_times:
            self.clock.now = at
            progress.tick()
        if self.service_error is not None:
            raise self.service_error
        return list(self.work_ids)

    def _recalculate(self, **kwargs: Any) -> None:
        self.finished_before_recalculation.append(
            sum(1 for c in self.runs.calls if c[0] == "finish_stage")
        )

    def failed_rows(self) -> list[Any]:
        return [
            t
            for t in self.failure_progress.received_upserts
            if t.status == TaskStatus.FAILED and t.task_type == TaskType.MATCHING
        ]


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> _Rig:
    return _Rig(monkeypatch)


# Taken at import, before the rig puts a recorder in the identity task's place (the artist
# stage's hand-off target).
def _artist_stage() -> Callable[..., None]:
    run: Callable[..., None] = artist_matching_task.call_local
    return run


def _identity_stage() -> Callable[..., None]:
    run: Callable[..., None] = identity_matching_task.call_local
    return run


# --- progress while a stage runs ------------------------------------------------------------


@pytest.mark.parametrize("stage", [_artist_stage, _identity_stage])
def test_a_stage_refreshes_at_its_start_then_at_most_every_two_seconds(
    rig: _Rig, stage: Callable[[], Callable[..., None]]
) -> None:
    run_id = uuid4().hex
    rig.tick_times = [1.0, 2.5, 3.0, 4.6]

    stage()(str(uuid4()), run_id)

    # Start (t=0): 10 pending. Ticks settle 1..4; refreshes land at t=2.5 (2 settled) and
    # t=4.6 (4 settled, 2.1 s after the last); t=1.0 and t=3.0 are too soon.
    assert rig.runs.refreshed(run_id) == [10, 8, 6]


@pytest.mark.parametrize("stage", [_artist_stage, _identity_stage])
def test_a_stage_whose_start_count_failed_never_refreshes(
    rig: _Rig, stage: Callable[[], Callable[..., None]]
) -> None:
    run_id = uuid4().hex
    rig.pending = FakePendingMatchWork(None)
    rig.tick_times = [1.0, 3.0, 6.0]

    stage()(str(uuid4()), run_id)

    assert rig.runs.refreshed(run_id) == []


# --- the artist stage ----------------------------------------------------------------------


def test_the_artist_stage_hands_off_with_the_run_id_and_keeps_its_stage(rig: _Rig) -> None:
    playlist_id, run_id = str(uuid4()), uuid4().hex

    _artist_stage()(playlist_id, run_id)

    rig.identity.assert_called_once()
    call = rig.identity.call_args
    assert (call.args, call.kwargs) in [
        ((playlist_id, run_id), {}),
        ((playlist_id,), {"run_id": run_id}),
    ]
    assert rig.runs.finished(run_id) == []


def test_a_refused_hand_off_finishes_the_artist_stage_as_failed(rig: _Rig) -> None:
    run_id = uuid4().hex
    rig.identity.side_effect = sqlite3.OperationalError("database is locked")

    _artist_stage()(str(uuid4()), run_id)

    assert rig.runs.finished(run_id) == [("finish_stage", run_id, 4, True)]


def test_a_non_storage_hand_off_error_propagates_and_finishes_the_stage(rig: _Rig) -> None:
    run_id = uuid4().hex
    rig.identity.side_effect = RuntimeError("not a storage fault")

    with pytest.raises(RuntimeError, match="not a storage fault"):
        _artist_stage()(str(uuid4()), run_id)

    assert [c[3] for c in rig.runs.finished(run_id)] == [True]


def test_an_artist_stage_that_raises_finishes_failed_and_still_reports_itself(
    rig: _Rig,
) -> None:
    run_id = uuid4().hex
    rig.service_error = ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        _artist_stage()(str(uuid4()), run_id)

    assert rig.runs.finished(run_id) == [("finish_stage", run_id, 4, True)]
    rig.identity.assert_not_called()
    failed = rig.failed_rows()
    assert len(failed) == 1
    assert failed[0].task_id != run_id


# --- the identity stage --------------------------------------------------------------------


def test_the_identity_stage_finishes_its_stage_with_a_fresh_count(rig: _Rig) -> None:
    run_id = uuid4().hex
    rig.tick_times = [1.0]

    _identity_stage()(str(uuid4()), run_id)

    assert rig.runs.finished(run_id) == [("finish_stage", run_id, 4, False)]


def test_the_identity_stage_finishes_after_recalculating_song_masters(rig: _Rig) -> None:
    run_id = uuid4().hex
    rig.work_ids = ["work-1"]

    _identity_stage()(str(uuid4()), run_id)

    assert rig.finished_before_recalculation == [0]
    assert len(rig.runs.finished(run_id)) == 1


def test_the_last_identity_stage_completes_the_run(rig: _Rig) -> None:
    run_id = uuid4().hex
    rig.runs.open(run_id, total=10, stages=1)
    rig.pending = FakePendingMatchWork(10, 0)

    _identity_stage()(str(uuid4()), run_id)

    row = rig.runs.rows[run_id]
    assert row.status == TaskStatus.COMPLETED
    assert row.progress_data["processed"] == 10
    assert "warning" not in row.progress_data


def test_an_identity_stage_that_raises_finishes_failed_and_still_reports_itself(
    rig: _Rig,
) -> None:
    run_id = uuid4().hex
    rig.service_error = ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        _identity_stage()(str(uuid4()), run_id)

    assert rig.runs.finished(run_id) == [("finish_stage", run_id, 4, True)]
    failed = rig.failed_rows()
    assert len(failed) == 1
    assert failed[0].task_id != run_id


def test_a_failed_fresh_count_falls_back_to_start_count_less_ticks(rig: _Rig) -> None:
    run_id = uuid4().hex
    rig.pending = FakePendingMatchWork(10, None)
    rig.tick_times = [0.5, 0.6, 0.7]

    _identity_stage()(str(uuid4()), run_id)

    assert rig.runs.finished(run_id) == [("finish_stage", run_id, 7, False)]


def test_with_no_count_at_all_the_stage_finishes_with_an_unknown_count(rig: _Rig) -> None:
    run_id = uuid4().hex
    rig.pending = FakePendingMatchWork(None)
    rig.tick_times = [0.5]

    _identity_stage()(str(uuid4()), run_id)

    assert rig.runs.finished(run_id) == [("finish_stage", run_id, None, False)]


# --- no run id: today's behaviour ------------------------------------------------------------


def test_an_artist_stage_with_no_run_id_never_touches_the_run(rig: _Rig) -> None:
    playlist_id = str(uuid4())
    rig.tick_times = [1.0, 5.0]

    _artist_stage()(playlist_id)

    assert rig.ports_opened == 0
    assert rig.runs.calls == []
    rig.identity.assert_called_once_with(playlist_id)


def test_an_identity_stage_with_no_run_id_never_touches_the_run(rig: _Rig) -> None:
    rig.tick_times = [1.0, 5.0]

    _identity_stage()(str(uuid4()))

    assert rig.ports_opened == 0
    assert rig.runs.calls == []
