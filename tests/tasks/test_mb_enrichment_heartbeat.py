"""Heartbeat tests for the batch lookups' ``on_progress`` and ``_emit_pre_pass_heartbeat``.

These tests pin the behavior described in the WS-empty-during-mb_enrichment
fix plan ("Public contract" + "Tests to add" sections). Two layers of test:

1. Client-level (``search_artists_by_mbids`` / ``lookup_recordings`` on a fake
   client with a capturing `on_progress` callable that records calls). Verifies
   the once-per-MBID / ordering / coverage invariants without involving
   `_PhaseContext` or repositories.

2. Production-callback-level (`_emit_pre_pass_heartbeat` driving a
   `FakeTaskProgressRepository`). Verifies the overlay shape, the
   `-prepass` phase suffix, and the swallow-on-psycopg-error semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.repositories.task_progress import TaskProgressRepository
from backend.tasks.mb_enrichment_tasks import (
    _emit_pre_pass_heartbeat,
    _PhaseContext,
)
from tests.fakes.mb_client import FakeMbClient
from tests.fakes.task_progress import FakeTaskProgressRepository

# ---------------------------------------------------------------------------
# Client-level tests — the batch lookups with a capturing callback
# ---------------------------------------------------------------------------


class _CaptureCallback:
    """Records (current, total, mbid) tuples for each `on_progress` call."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str]] = []

    def __call__(self, current: int, total: int, mbid: str) -> None:
        self.calls.append((current, total, mbid))


def _all_success_artists(mbids: list[str]) -> FakeMbClient:
    return FakeMbClient(artists={m: {"id": m, "name": f"name-{m}"} for m in mbids})


def _all_success_recordings(mbids: list[str]) -> FakeMbClient:
    return FakeMbClient(recordings={m: {"id": m, "title": f"t-{m}"} for m in mbids})


def _all_failure_recordings(mbids: list[str]) -> FakeMbClient:
    return FakeMbClient(error_mbids=set(mbids))


# ---- Cardinality invariants -----------------------------------------------


def test_artist_heartbeat_once_per_mbid_success_path() -> None:
    mbids = [f"mbid-a-{i:04x}" for i in range(50)]
    cb = _CaptureCallback()
    _all_success_artists(mbids).search_artists_by_mbids(sorted(mbids), on_progress=cb)
    assert len(cb.calls) == len(set(mbids))
    assert {total for _, total, _ in cb.calls} == {len(set(mbids))}


def test_recording_heartbeat_once_per_mbid_success_path() -> None:
    mbids = [f"mbid-r-{i:04x}" for i in range(50)]
    cb = _CaptureCallback()
    _all_success_recordings(mbids).lookup_recordings(sorted(mbids), on_progress=cb)
    assert len(cb.calls) == len(set(mbids))
    assert {total for _, total, _ in cb.calls} == {len(set(mbids))}


def test_recording_heartbeat_once_per_mbid_failure_path() -> None:
    """Heartbeats still fire when every lookup raises httpx.HTTPError.

    A streak of failing MBIDs must not re-create the >10-min heartbeat gap
    that was the original WS-empty bug.
    """
    mbids = [f"mbid-r-{i:04x}" for i in range(50)]
    cb = _CaptureCallback()
    _all_failure_recordings(mbids).lookup_recordings(sorted(mbids), on_progress=cb)
    assert len(cb.calls) == len(set(mbids))


# ---- Order invariant ------------------------------------------------------


def test_artist_heartbeat_order_follows_input_order() -> None:
    """Heartbeats come in input order, numbered 1..N, one per distinct MBID."""
    mbids = ["mbid-a", "mbid-c", "mbid-m", "mbid-z"]
    client = FakeMbClient(
        artists={
            "mbid-a": {"id": "mbid-a", "name": "n"},
            "mbid-m": {"id": "mbid-m", "name": "n"},
        },
    )
    cb = _CaptureCallback()
    client.search_artists_by_mbids(mbids, on_progress=cb)

    assert cb.calls == [(i, 4, m) for i, m in enumerate(mbids, start=1)]


def test_recording_heartbeat_order_follows_input_order_under_mixed_outcomes() -> None:
    mbids = ["mbid-a", "mbid-c", "mbid-m", "mbid-z"]
    client = FakeMbClient(
        recordings={
            "mbid-a": {"id": "mbid-a", "title": "t"},
            "mbid-m": {"id": "mbid-m", "title": "t"},
        },
        error_mbids={"mbid-z", "mbid-c"},
    )
    cb = _CaptureCallback()
    client.lookup_recordings(mbids, on_progress=cb)

    assert cb.calls == [(i, 4, m) for i, m in enumerate(mbids, start=1)]


# ---- Per-MBID coverage ----------------------------------------------------


def test_artist_heartbeat_covers_every_mbid() -> None:
    """No MBID is silently skipped, and none is reported twice."""
    mbids = [f"mbid-a-{i}" for i in range(20)]
    cb = _CaptureCallback()
    _all_success_artists(mbids).search_artists_by_mbids(sorted(mbids), on_progress=cb)
    seen = [m for _, _, m in cb.calls]
    assert sorted(seen) == sorted(mbids)


def test_recording_heartbeat_covers_every_mbid() -> None:
    mbids = [f"mbid-r-{i}" for i in range(20)]
    cb = _CaptureCallback()
    _all_success_recordings(mbids).lookup_recordings(sorted(mbids), on_progress=cb)
    seen = [m for _, _, m in cb.calls]
    assert sorted(seen) == sorted(mbids)


# ---- Duplicate input handling --------------------------------------------


def test_recording_prepass_total_is_distinct_count_not_input_count() -> None:
    """The `total` argument to `on_progress` reflects DISTINCT MBIDs, not the
    raw input length, so callers never see `prepass_total = |input|`.
    """
    inputs = ["mbid-A", "mbid-A", "mbid-B", "mbid-A", "mbid-C"]
    cb = _CaptureCallback()
    _all_success_recordings(["mbid-A", "mbid-B", "mbid-C"]).lookup_recordings(
        inputs, on_progress=cb
    )
    totals = {t for _, t, _ in cb.calls}
    assert totals == {3}, "prepass_total must equal |distinct|, not |input|"


# ---- Callback raise = propagate (no swallow at the client) ---------------


def test_client_propagates_callback_exceptions() -> None:
    """`on_progress` callbacks MUST NOT raise. If one does, the client does not
    swallow it: the public contract treats a raising callback as a logic bug
    worth surfacing, not an observability hiccup to ignore.
    `_emit_pre_pass_heartbeat` is the production callback; it satisfies the
    no-raise contract by catching `psycopg.Error` internally.
    """

    class RaisingCallback:
        def __call__(self, current: int, total: int, mbid: str) -> None:
            raise RuntimeError("logic bug in callback")

    with pytest.raises(RuntimeError, match="logic bug in callback"):
        _all_success_artists(["mbid-a"]).search_artists_by_mbids(
            ["mbid-a"], on_progress=RaisingCallback()
        )


# ---------------------------------------------------------------------------
# Production-callback-level tests — `_emit_pre_pass_heartbeat`
# ---------------------------------------------------------------------------


def _seed_running_row(repo: FakeTaskProgressRepository, task_id: str) -> None:
    """Seed an initial RUNNING row so `touch_running` (which UPDATEs by id)
    has a row to merge into, mirroring the production initial upsert at
    `mb_enrichment_task` task entry.
    """
    now = datetime.now(tz=UTC)
    repo.upsert(
        TaskProgress(
            task_id=task_id,
            task_type=TaskType.MB_ENRICHMENT,
            status=TaskStatus.RUNNING,
            progress_data={"processed": 627, "total": 17193, "phase": "recordings"},
            started_at=now,
            updated_at=now,
            completed_at=None,
        )
    )


def _make_ctx(repo: TaskProgressRepository, task_id: str = "task-1") -> _PhaseContext:
    return _PhaseContext(
        task_id=task_id,
        task_started_at=datetime.now(tz=UTC),
        total=17193,
        progress_repo=repo,
        processed=627,
    )


def test_emit_pre_pass_heartbeat_writes_correct_overlay_shape() -> None:
    """Overlay carries `current_item`, `phase`, `prepass_current`,
    `prepass_total` — and nothing else. Specifically MUST NOT carry
    `processed`, `total`, `started_at`, or `error`.
    """
    repo = FakeTaskProgressRepository()
    _seed_running_row(repo, "task-1")
    ctx = _make_ctx(repo)

    _emit_pre_pass_heartbeat(ctx, "recordings", current=42, total=100, mbid="37c60ed7-deadbeef")

    assert len(repo.received_touches) == 1
    task_id, overlay = repo.received_touches[0]
    assert task_id == "task-1"
    assert overlay == {
        "phase": "recordings-prepass",
        "current_item": "prepass:37c60ed7-deadbeef",
        "prepass_current": 42,
        "prepass_total": 100,
    }
    forbidden_keys = {"processed", "total", "started_at", "error"}
    assert forbidden_keys.isdisjoint(overlay.keys())


def test_emit_pre_pass_heartbeat_appends_prepass_suffix_only_once() -> None:
    """`phase` arg is the BASE phase string. Helper appends `-prepass`.

    Callers MUST NOT pass `recordings-prepass` themselves (would yield
    `recordings-prepass-prepass`). Documenting the contract via test.
    """
    repo = FakeTaskProgressRepository()
    _seed_running_row(repo, "task-1")
    ctx = _make_ctx(repo)

    _emit_pre_pass_heartbeat(ctx, "artists", current=1, total=1, mbid="mbid-x")
    _, overlay_a = repo.received_touches[-1]
    assert overlay_a["phase"] == "artists-prepass"

    _emit_pre_pass_heartbeat(ctx, "recordings", current=1, total=1, mbid="mbid-y")
    _, overlay_r = repo.received_touches[-1]
    assert overlay_r["phase"] == "recordings-prepass"


def test_emit_pre_pass_heartbeat_does_not_mutate_processed() -> None:
    """Heartbeats refresh updated_at + overlay only — `ctx.processed` and
    the row's persisted `processed` key are owned by `_advance_progress`
    and the initial upsert. A heartbeat must never bump them.
    """
    repo = FakeTaskProgressRepository()
    _seed_running_row(repo, "task-1")
    ctx = _make_ctx(repo)
    pre_processed = ctx.processed

    for i in range(50):
        _emit_pre_pass_heartbeat(ctx, "recordings", current=i + 1, total=50, mbid=f"mbid-{i}")

    assert ctx.processed == pre_processed
    persisted = repo.get_by_id("task-1")
    assert persisted is not None
    assert persisted.progress_data["processed"] == 627
    assert persisted.progress_data["total"] == 17193


def test_emit_pre_pass_heartbeat_swallows_psycopg_error_and_logs() -> None:
    """A transient psycopg.Error from `touch_running` must NOT propagate.

    The pre-pass continues, a structured warning is logged with
    `event=heartbeat_failed`. If `progress_conn` is genuinely dead, the
    next per-item `_advance_progress` will surface it (it propagates
    psycopg errors), so the swallow is bounded by pre-pass duration.
    """

    class FlakyRepo(FakeTaskProgressRepository):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def touch_running(self, task_id: str, progress_overlay: dict[str, Any]) -> int:
            self.calls += 1
            if self.calls == 5:
                raise psycopg.OperationalError("simulated DB blip")
            return super().touch_running(task_id, progress_overlay)

    repo = FlakyRepo()
    _seed_running_row(repo, "task-1")
    ctx = _make_ctx(repo)

    with capture_logs() as logs:
        for i in range(10):
            _emit_pre_pass_heartbeat(ctx, "recordings", current=i + 1, total=10, mbid=f"mbid-{i}")

    assert repo.calls == 10, "subsequent heartbeats must continue after the blip"
    heartbeat_failed_logs = [e for e in logs if e.get("event") == "heartbeat_failed"]
    assert len(heartbeat_failed_logs) == 1
    assert heartbeat_failed_logs[0]["task_id"] == "task-1"
    assert heartbeat_failed_logs[0]["phase"] == "recordings-prepass"


def test_emit_pre_pass_heartbeat_logs_when_rowcount_zero() -> None:
    """Missing row → log `event=touch_running_no_row` ERROR and continue.

    The row is self-healing on the next `_advance_progress` upsert
    (INSERT ON CONFLICT path), so crashing the task here would defeat the
    swallow-and-log philosophy.
    """
    repo = FakeTaskProgressRepository()
    # Deliberately do NOT seed: no row exists, touch_running returns 0.
    ctx = _make_ctx(repo)

    with capture_logs() as logs:
        _emit_pre_pass_heartbeat(ctx, "recordings", current=1, total=1, mbid="mbid-x")

    no_row_logs = [e for e in logs if e.get("event") == "touch_running_no_row"]
    assert len(no_row_logs) == 1
    assert no_row_logs[0]["task_id"] == "task-1"
    assert no_row_logs[0]["phase"] == "recordings-prepass"


def test_emit_pre_pass_heartbeat_resurrects_failed_row() -> None:
    """Resurrection contract: a heartbeat brings a WS-tentatively-failed row
    back to RUNNING.

    This is the central behavior the whole fix exists to provide. The WS
    stale-cleanup (`STALE_SQL` in backend/websocket.py) tentatively flips a row to
    `failed` after 10 minutes without `updated_at` advance. If the worker
    is still running and emits a heartbeat afterwards, the row MUST go
    back to `running` so the WS broadcast picks it up again.
    """
    repo = FakeTaskProgressRepository()
    _seed_running_row(repo, "task-1")
    # Simulate WS stale-cleanup tentatively flipping the row.
    existing = repo.get_by_id("task-1")
    assert existing is not None
    repo.upsert(
        TaskProgress(
            task_id="task-1",
            task_type=TaskType.MB_ENRICHMENT,
            status=TaskStatus.FAILED,
            progress_data=existing.progress_data,
            started_at=existing.started_at,
            updated_at=existing.updated_at,
            completed_at=datetime.now(tz=UTC),
        )
    )
    assert repo.get_by_id("task-1").status == TaskStatus.FAILED  # type: ignore[union-attr]

    ctx = _make_ctx(repo)
    _emit_pre_pass_heartbeat(ctx, "recordings", current=1, total=1, mbid="mbid-x")

    resurrected = repo.get_by_id("task-1")
    assert resurrected is not None
    assert resurrected.status == TaskStatus.RUNNING
    assert resurrected.progress_data["phase"] == "recordings-prepass"
    assert resurrected.progress_data["prepass_current"] == 1
    # Persistent keys from the initial upsert survive the JSONB merge.
    assert resurrected.progress_data["processed"] == 627
    assert resurrected.progress_data["total"] == 17193
    # Resurrection MUST clear completed_at so a future defensive guard
    # like `AND completed_at IS NULL` on the WS running branch wouldn't
    # silently break the broadcast. The WS stale-cleanup set it; the
    # heartbeat clears it.
    assert resurrected.completed_at is None, (
        "touch_running must clear completed_at when resurrecting a failed row"
    )
