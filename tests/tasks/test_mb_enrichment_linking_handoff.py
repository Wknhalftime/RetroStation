"""Acceptance tests: mb_enrichment_task queues the artist linking before the re-check (D15).

After its task_run exits normally, mb_enrichment_task queues link_local_artists_task() and then
rematch_undecided_task("changed"). Each goes through enqueue_or_log, on one fresh autocommit
connection. On -w 1 the queue runs them in that order, so the re-check matches against the new
MBIDs (AUD-R026). A refused linking enqueue is logged on the MB run and does not stop the
re-check. A FAILED MB run queues neither.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from backend.domain.enums import TaskStatus
from tests.fakes.system_logs import FakeSystemLogRepository
from tests.fakes.task_progress import FakeTaskProgressRepository


def _mk_conn() -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = MagicMock()
    cur.rowcount = 0
    conn.execute.return_value = cur
    return conn


def _empty_queues(_conn: object) -> MagicMock:
    repos = MagicMock()
    repos.artists.list_unenhanced.return_value = []
    repos.works.list_needing_enhancement.return_value = []
    repos.recordings.list_needing_enhancement.return_value = []
    return repos


class _Rig:
    def __init__(self) -> None:
        self.progress = FakeTaskProgressRepository()
        self.logs = FakeSystemLogRepository()
        self.handoff_logs = FakeSystemLogRepository()
        self.connects: list[dict[str, Any]] = []
        self.order: list[str] = []
        self.attempts = 0
        self.fail_connect = False
        self.refuse_link = False

    def connect(self, _url: str, **kwargs: Any) -> MagicMock:
        self.attempts += 1
        if self.fail_connect and self.attempts == 1:
            raise RuntimeError("pre-count boom")
        self.connects.append(kwargs)
        return _mk_conn()

    def link(self) -> None:
        if self.refuse_link:
            raise sqlite3.OperationalError("database is locked")
        self.order.append("link")

    def recheck(self, scope: str) -> None:
        self.order.append(f"recheck:{scope}")


@pytest.fixture
def rig() -> Iterator[_Rig]:
    r = _Rig()
    with (
        patch("backend.tasks._task_run.connect_sync", return_value=MagicMock()),
        patch("backend.tasks._task_run.PgTaskProgressRepository", return_value=r.progress),
        patch("backend.tasks._task_run.PgSystemLogRepository", return_value=r.logs),
        patch("backend.tasks.mb_enrichment_tasks.connect_sync", side_effect=r.connect),
        patch("backend.tasks.mb_enrichment_tasks.RepositoryFactory", side_effect=_empty_queues),
        patch("backend.tasks.mb_enrichment_tasks.PgMusicBrainzCacheRepository"),
        patch("backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient") as mb_cls,
        patch(
            "backend.tasks.mb_enrichment_tasks.PgSystemLogRepository",
            return_value=r.handoff_logs,
        ),
        patch(
            "backend.tasks.artist_linking_tasks.link_local_artists_task",
            side_effect=r.link,
        ),
        patch(
            "backend.tasks.matching_recheck_tasks.rematch_undecided_task",
            side_effect=r.recheck,
        ),
    ):
        mb_cls.return_value.__enter__ = lambda self: self
        mb_cls.return_value.__exit__ = lambda self, *exc: False
        yield r


def _run() -> dict[str, int]:
    from backend.tasks.mb_enrichment_tasks import mb_enrichment_task

    result: dict[str, int] = mb_enrichment_task.call_local()
    return result


def test_a_completed_mb_run_queues_the_linking_then_the_recheck(rig: _Rig) -> None:
    _run()

    assert rig.order == ["link", "recheck:changed"]


def test_both_hand_offs_share_one_fresh_autocommit_connection(rig: _Rig) -> None:
    _run()

    assert [c for c in rig.connects if c.get("autocommit")] == [{"autocommit": True}]
    assert rig.connects[-1] == {"autocommit": True}


def test_a_refused_linking_hand_off_is_logged_and_the_recheck_still_queues(rig: _Rig) -> None:
    rig.refuse_link = True

    _run()  # must not raise: the caller owns the hand-off (AUD-R012 (1))

    assert rig.order == ["recheck:changed"]
    completed = [u for u in rig.progress.received_upserts if u.status == TaskStatus.COMPLETED]
    assert len(completed) == 1
    assert TaskStatus.FAILED not in [u.status for u in rig.progress.received_upserts]
    refused = [
        log
        for log in rig.handoff_logs.all
        if log.message == "link_local_artists_task_enqueue_failed"
    ]
    assert len(refused) == 1
    assert refused[0].trace_id == completed[0].task_id


def test_a_failed_mb_run_queues_neither(rig: _Rig) -> None:
    rig.fail_connect = True

    with pytest.raises(RuntimeError, match="pre-count boom"):
        _run()

    assert rig.order == []
