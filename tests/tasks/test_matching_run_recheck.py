"""Acceptance tests: the re-check opens the matching run's row (spec 2026-10-17).

When ``rematch_undecided_task`` has playlists with pending work it:
- counts the pending artists + songs after the rewind is committed, through the run ports;
- opens one run row (``open(run_id, total=<count>, stages=<playlists>)``) before its own
  ``matching_recheck`` row is COMPLETED, so the bottom bar never has a gap;
- queues ``artist_matching_task(playlist_id, run_id)`` for every playlist, with one run id, a
  uuid4 hex;
- finishes one stage as failed for each enqueue the Huey store refuses;
- when it fails after opening the row, finishes every stage it did not queue as failed, so the
  row closes instead of waiting for the ``/ws`` reaper; the error still propagates.

With no playlist to queue it opens no row.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import psycopg
import pytest

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import MatchStatus, TaskStatus, TaskType
from backend.domain.system import TaskProgress
from tests.fakes.artist_linking import FakeArtistLinkingRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matching_run import FakeMatchingRunRepository
from tests.fakes.pending_match_work import FakePendingMatchWork
from tests.fakes.system_logs import FakeSystemLogRepository
from tests.fakes.task_progress import FakeTaskProgressRepository


class _OrderedProgress(FakeTaskProgressRepository):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    def upsert(self, task: TaskProgress) -> TaskProgress:
        if task.task_type == TaskType.MATCHING_RECHECK and task.status == TaskStatus.COMPLETED:
            self._order.append("recheck_completed")
        return super().upsert(task)


class _OrderedRuns(FakeMatchingRunRepository):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    def open(self, run_id: str, *, total: int, stages: int) -> None:
        self._order.append("open")
        super().open(run_id, total=total, stages=stages)


class _OrderedPending(FakePendingMatchWork):
    def __init__(self, order: list[str], value: int) -> None:
        super().__init__(value)
        self._order = order

    def count_pending(self) -> int | None:
        self._order.append("count")
        return super().count_pending()


class _Rig:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.progress = _OrderedProgress(self.order)
        self.logs = FakeSystemLogRepository()
        self.handoff_logs = FakeSystemLogRepository()
        self.files = FakeLibraryFileRepository()
        self.artists = FakeBroadcastArtistRepository()
        self.songs = FakeBroadcastTrackIdentityRepository()
        self.runs = _OrderedRuns(self.order)
        self.pending = _OrderedPending(self.order, 7)
        self.queued: list[tuple[str, str | None]] = []
        self.refuse: set[str] = set()
        self.fanout_connect_error: Exception | None = None
        self.conn = MagicMock()
        self.conn.__enter__.return_value = self.conn
        self.conn.__exit__.return_value = False
        self.conn.commit.side_effect = lambda: self.order.append("commit")

    def connect(self, _url: str, **kwargs: Any) -> MagicMock:
        if kwargs.get("autocommit") and self.fanout_connect_error is not None:
            raise self.fanout_connect_error
        return self.conn

    def factory(self, _conn: object) -> SimpleNamespace:
        return SimpleNamespace(
            library_files=self.files,
            broadcast_artists=self.artists,
            broadcast_identities=self.songs,
            task_progress=self.progress,
            system_logs=self.handoff_logs,
            artist_linking=FakeArtistLinkingRepository(),
        )

    def enqueue(self, playlist_id: str, run_id: str | None = None) -> None:
        self.order.append("enqueue")
        if playlist_id in self.refuse:
            raise sqlite3.OperationalError("database is locked")
        self.queued.append((playlist_id, run_id))

    @contextmanager
    def run_ports(self, _database_url: str) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(runs=self.runs, pending=self.pending, clock=lambda: 0.0)

    def playlist_with_pending_song(self) -> UUID:
        playlist_id = uuid4()
        artist = self.artists.upsert(
            BroadcastArtist(id=uuid4(), original_name="abba", normalized_name="abba")
        )
        title = uuid4().hex
        song = self.songs.upsert(
            BroadcastTrackIdentity(
                id=uuid4(),
                broadcast_artist_id=artist.id,
                original_title=title,
                normalized_title=title,
                normalized_signature=f"abba:{title}",
                match_status=MatchStatus.PENDING,
            )
        )
        self.songs.register_playlist_identity(playlist_id, song.id)
        self.artists.register_playlist_artist(playlist_id, artist.id)
        return playlist_id

    def only_run(self) -> TaskProgress:
        assert len(self.runs.rows) == 1, f"expected one run row, got {len(self.runs.rows)}"
        return next(iter(self.runs.rows.values()))


@pytest.fixture
def rig() -> Iterator[_Rig]:
    r = _Rig()
    with (
        patch("backend.tasks._task_run.connect_sync", return_value=MagicMock()),
        patch("backend.tasks._task_run.PgTaskProgressRepository", return_value=r.progress),
        patch("backend.tasks._task_run.PgSystemLogRepository", return_value=r.logs),
        patch("backend.tasks.matching_recheck_tasks.connect_sync", side_effect=r.connect),
        patch("backend.tasks.matching_recheck_tasks.RepositoryFactory", side_effect=r.factory),
        patch(
            "backend.tasks.matching_recheck_tasks.PgSystemLogRepository",
            return_value=r.handoff_logs,
            create=True,
        ),
        patch("backend.tasks.artist_matching_tasks.artist_matching_task", side_effect=r.enqueue),
        patch("backend.tasks._matching_run.run_ports", side_effect=r.run_ports),
        # A tripwire: these tests never reach a database (the dev DB is the default URL).
        patch(
            "backend.tasks._matching_run.connect_sync",
            side_effect=AssertionError("a unit test reached a database"),
        ),
    ):
        yield r


def _run(scope: str = "all") -> dict[str, int]:
    from backend.tasks.matching_recheck_tasks import rematch_undecided_task

    result: dict[str, int] = rematch_undecided_task.call_local(scope)
    return result


# --- opening the run ------------------------------------------------------------------------


def test_the_recheck_opens_one_run_row_counted_after_the_rewind(rig: _Rig) -> None:
    rig.playlist_with_pending_song()
    rig.playlist_with_pending_song()

    _run()

    row = rig.only_run()
    assert row.task_type == TaskType.MATCHING
    assert row.status == TaskStatus.RUNNING
    assert row.progress_data == {"processed": 0, "total": 7, "stages_left": 2, "stages_failed": 0}
    assert re.fullmatch(r"[0-9a-f]{32}", row.task_id)


def test_the_count_follows_the_commit_and_the_row_opens_before_the_recheck_completes(
    rig: _Rig,
) -> None:
    rig.playlist_with_pending_song()

    _run()

    order = rig.order
    assert order.index("commit") < order.index("count") < order.index("open")
    assert order.index("open") < order.index("recheck_completed")
    assert order.index("open") < order.index("enqueue")


def test_every_playlist_is_queued_with_the_run_id(rig: _Rig) -> None:
    first = rig.playlist_with_pending_song()
    second = rig.playlist_with_pending_song()

    _run()

    run_id = rig.only_run().task_id
    assert sorted(rig.queued) == sorted([(str(first), run_id), (str(second), run_id)])


def test_a_refused_enqueue_finishes_its_stage_as_failed(rig: _Rig) -> None:
    refused = rig.playlist_with_pending_song()
    rig.playlist_with_pending_song()
    rig.refuse.add(str(refused))

    _run()

    row = rig.only_run()
    assert [c[3] for c in rig.runs.finished(row.task_id)] == [True]
    assert row.status == TaskStatus.RUNNING
    assert (row.progress_data["stages_left"], row.progress_data["stages_failed"]) == (1, 1)


def test_when_every_enqueue_is_refused_the_run_closes_with_the_warning(rig: _Rig) -> None:
    for _ in range(2):
        rig.refuse.add(str(rig.playlist_with_pending_song()))

    _run()

    row = rig.only_run()
    assert row.status == TaskStatus.COMPLETED
    assert row.progress_data["stages_failed"] == 2
    assert row.progress_data["warning"] == "matching_stages_failed"


def test_no_playlist_to_queue_opens_no_run(rig: _Rig) -> None:
    _run()

    assert rig.runs.rows == {}
    assert rig.queued == []


# --- the re-check failing after it opened the run --------------------------------------------


def test_a_fanout_that_fails_after_opening_closes_the_run(rig: _Rig) -> None:
    rig.playlist_with_pending_song()
    rig.playlist_with_pending_song()
    rig.fanout_connect_error = psycopg.OperationalError("connection refused")

    with pytest.raises(psycopg.OperationalError):
        _run()

    row = rig.only_run()
    assert rig.queued == []
    assert row.status == TaskStatus.COMPLETED
    assert row.progress_data["stages_failed"] == 2
    assert row.progress_data["warning"] == "matching_stages_failed"


def test_a_recheck_whose_completed_row_fails_after_opening_closes_the_run(rig: _Rig) -> None:
    rig.playlist_with_pending_song()

    def refuse_completed(task: TaskProgress) -> TaskProgress:
        if task.task_type == TaskType.MATCHING_RECHECK and task.status == TaskStatus.COMPLETED:
            raise psycopg.OperationalError("server closed the connection")
        return FakeTaskProgressRepository.upsert(rig.progress, task)

    with (
        patch.object(rig.progress, "upsert", side_effect=refuse_completed),
        pytest.raises(psycopg.OperationalError),
    ):
        _run()

    row = rig.only_run()
    assert rig.queued == []
    assert row.status == TaskStatus.COMPLETED
    assert row.progress_data["stages_failed"] == 1
