"""Acceptance tests: the matching run's progress row (spec 2026-10-17 "Matching run progress").

One RUNNING ``matching`` row per run, in ``progress_tracking``, so the bottom bar, the disabled
Re-run button and the end-of-run queue refresh light up:

- ``open`` writes it: ``{processed: 0, total, stages_left, stages_failed: 0}``;
- ``refresh(pending)``: ``total' = max(total, processed + pending)``, ``processed' = total' -
  pending``, so new work grows the total and ``processed`` never goes back;
- ``finish_stage(pending, failed)``: the same count (``pending`` None keeps ``processed``),
  one stage fewer, one more failed stage when ``failed``. At no stage left the row is
  COMPLETED with ``completed_at``, and ``warning = "matching_stages_failed"`` when any stage
  failed, never otherwise;
- a row the ``/ws`` reaper failed comes back RUNNING, ``completed_at`` cleared and
  ``updated_at`` moved on; a COMPLETED row and an unknown id are left alone;
- the Pg adapter never raises for a database it cannot reach: progress is telemetry.

Each scenario runs against the in-memory fake and against PostgreSQL (``integration``).
``PendingMatchWork`` counts pending broadcast artists plus pending broadcast songs, and gives
None, not an error, when the database cannot answer. Both Pg adapters take a zero-arg
``connect`` callable (they open their connection when first used) and have ``close()``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.repositories.matching_run import MatchingRunRepository
from tests.fakes.matching_run import FakeMatchingRunRepository

WARNING = "matching_stages_failed"


@dataclass
class _Backend:
    repo: MatchingRunRepository
    read: Callable[[str], TaskProgress | None]
    reap: Callable[[str], datetime]


def _fake_backend() -> Iterator[_Backend]:
    repo = FakeMatchingRunRepository()
    yield _Backend(repo=repo, read=repo.rows.get, reap=repo.reap)


def _pg_backend(url: str) -> Iterator[_Backend]:
    from backend.db.repositories.matching_run import PgMatchingRunRepository
    from backend.db.repositories.task_progress import PgTaskProgressRepository

    reader = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    repo = PgMatchingRunRepository(
        lambda: psycopg.connect(url, autocommit=True, row_factory=dict_row)
    )

    def read(run_id: str) -> TaskProgress | None:
        return PgTaskProgressRepository(reader).get_by_id(run_id)

    def reap(run_id: str) -> datetime:
        row = reader.execute(
            """UPDATE progress_tracking
                  SET status = 'failed', completed_at = now(),
                      updated_at = now() - interval '20 minutes'
                WHERE task_id = %s
            RETURNING updated_at""",
            (run_id,),
        ).fetchone()
        assert row is not None
        reaped_at: datetime = row["updated_at"]
        return reaped_at

    try:
        yield _Backend(repo=repo, read=read, reap=reap)
    finally:
        repo.close()
        reader.close()


@pytest.fixture(params=["fake", pytest.param("pg", marks=pytest.mark.integration)])
def backend(request: pytest.FixtureRequest) -> Iterator[_Backend]:
    if request.param == "fake":
        yield from _fake_backend()
    else:
        yield from _pg_backend(request.getfixturevalue("migrated_db"))


def _row(backend: _Backend, run_id: str) -> TaskProgress:
    row = backend.read(run_id)
    assert row is not None, "no row for the run"
    return row


def _counts(row: TaskProgress) -> dict[str, Any]:
    return {k: row.progress_data[k] for k in ("processed", "total", "stages_left", "stages_failed")}


# --- open ----------------------------------------------------------------------------------


def test_open_writes_one_running_matching_row(backend: _Backend) -> None:
    run_id = uuid4().hex

    backend.repo.open(run_id, total=10, stages=2)

    row = _row(backend, run_id)
    assert row.task_type == TaskType.MATCHING
    assert row.status == TaskStatus.RUNNING
    assert row.completed_at is None
    assert row.progress_data == {"processed": 0, "total": 10, "stages_left": 2, "stages_failed": 0}


# --- refresh -------------------------------------------------------------------------------


def test_refresh_counts_settled_items_and_grows_the_total_for_new_work(
    backend: _Backend,
) -> None:
    run_id = uuid4().hex
    backend.repo.open(run_id, total=10, stages=2)

    backend.repo.refresh(run_id, pending=7)
    assert _counts(_row(backend, run_id)) == {
        "processed": 3,
        "total": 10,
        "stages_left": 2,
        "stages_failed": 0,
    }

    # An upload added work: the total grows, processed stays where it was.
    backend.repo.refresh(run_id, pending=12)
    assert _counts(_row(backend, run_id))["processed"] == 3
    assert _counts(_row(backend, run_id))["total"] == 15

    backend.repo.refresh(run_id, pending=2)
    row = _row(backend, run_id)
    assert (row.progress_data["processed"], row.progress_data["total"]) == (13, 15)
    assert row.status == TaskStatus.RUNNING


def test_processed_never_goes_back(backend: _Backend) -> None:
    run_id = uuid4().hex
    backend.repo.open(run_id, total=10, stages=1)
    backend.repo.refresh(run_id, pending=4)

    backend.repo.refresh(run_id, pending=9)

    assert _row(backend, run_id).progress_data["processed"] == 6


# --- finish_stage --------------------------------------------------------------------------


def test_the_last_stage_completes_the_row(backend: _Backend) -> None:
    run_id = uuid4().hex
    backend.repo.open(run_id, total=10, stages=2)

    backend.repo.finish_stage(run_id, pending=4, failed=False)
    row = _row(backend, run_id)
    assert row.status == TaskStatus.RUNNING
    assert _counts(row) == {"processed": 6, "total": 10, "stages_left": 1, "stages_failed": 0}

    backend.repo.finish_stage(run_id, pending=0, failed=False)
    row = _row(backend, run_id)
    assert row.status == TaskStatus.COMPLETED
    assert row.completed_at is not None
    assert _counts(row) == {"processed": 10, "total": 10, "stages_left": 0, "stages_failed": 0}
    assert "warning" not in row.progress_data


def test_a_run_with_a_failed_stage_completes_with_the_warning(backend: _Backend) -> None:
    run_id = uuid4().hex
    backend.repo.open(run_id, total=5, stages=2)

    backend.repo.finish_stage(run_id, pending=3, failed=True)
    assert _row(backend, run_id).status == TaskStatus.RUNNING

    backend.repo.finish_stage(run_id, pending=3, failed=False)
    row = _row(backend, run_id)
    assert row.status == TaskStatus.COMPLETED
    assert row.progress_data["stages_failed"] == 1
    assert row.progress_data["warning"] == WARNING


def test_an_unknown_pending_count_keeps_processed(backend: _Backend) -> None:
    run_id = uuid4().hex
    backend.repo.open(run_id, total=10, stages=2)
    backend.repo.refresh(run_id, pending=6)

    backend.repo.finish_stage(run_id, pending=None, failed=False)

    row = _row(backend, run_id)
    assert _counts(row) == {"processed": 4, "total": 10, "stages_left": 1, "stages_failed": 0}


# --- the /ws reaper, a closed run, an unknown run -------------------------------------------


@pytest.mark.parametrize("write", ["refresh", "finish_stage"])
def test_a_reaped_row_comes_back_running(backend: _Backend, write: str) -> None:
    run_id = uuid4().hex
    backend.repo.open(run_id, total=10, stages=2)
    reaped_at = backend.reap(run_id)
    assert _row(backend, run_id).status == TaskStatus.FAILED

    if write == "refresh":
        backend.repo.refresh(run_id, pending=5)
    else:
        backend.repo.finish_stage(run_id, pending=5, failed=False)

    row = _row(backend, run_id)
    assert row.status == TaskStatus.RUNNING
    assert row.completed_at is None
    assert row.updated_at > reaped_at
    assert row.progress_data["processed"] == 5


def test_a_completed_row_is_left_alone(backend: _Backend) -> None:
    run_id = uuid4().hex
    backend.repo.open(run_id, total=3, stages=1)
    backend.repo.finish_stage(run_id, pending=0, failed=False)
    closed = _row(backend, run_id)

    backend.repo.refresh(run_id, pending=2)
    backend.repo.finish_stage(run_id, pending=2, failed=True)

    row = _row(backend, run_id)
    assert row.status == TaskStatus.COMPLETED
    assert row.progress_data == closed.progress_data
    assert row.completed_at == closed.completed_at


def test_an_unknown_run_writes_nothing(backend: _Backend) -> None:
    run_id = uuid4().hex

    backend.repo.refresh(run_id, pending=1)
    backend.repo.finish_stage(run_id, pending=1, failed=True)

    assert backend.read(run_id) is None


# --- Pg only: a database that cannot answer, or a statement that fails -----------------------


def _unreachable() -> psycopg.Connection[Any]:
    raise psycopg.OperationalError("connection refused")


def test_pg_run_writes_never_raise_for_an_unreachable_database() -> None:
    from backend.db.repositories.matching_run import PgMatchingRunRepository

    repo = PgMatchingRunRepository(_unreachable)

    repo.open(uuid4().hex, total=1, stages=1)
    repo.refresh(uuid4().hex, pending=1)
    repo.finish_stage(uuid4().hex, pending=None, failed=True)
    repo.close()


def test_pg_pending_count_is_none_for_an_unreachable_database() -> None:
    from backend.db.repositories.pending_match_work import PgPendingMatchWork

    pending = PgPendingMatchWork(_unreachable)

    assert pending.count_pending() is None
    pending.close()


def _failing_statements() -> MagicMock:
    """A live connection whose every statement fails (a statement or lock timeout)."""
    conn = MagicMock()
    conn.execute.side_effect = psycopg.errors.QueryCanceled("canceling statement due to timeout")
    return conn


def test_pg_run_writes_never_raise_for_a_failing_statement() -> None:
    from backend.db.repositories.matching_run import PgMatchingRunRepository

    repo = PgMatchingRunRepository(_failing_statements)

    repo.open(uuid4().hex, total=1, stages=1)
    repo.refresh(uuid4().hex, pending=1)
    repo.finish_stage(uuid4().hex, pending=None, failed=True)
    repo.close()


def test_pg_pending_count_is_none_for_a_failing_statement() -> None:
    from backend.db.repositories.pending_match_work import PgPendingMatchWork

    pending = PgPendingMatchWork(_failing_statements)

    assert pending.count_pending() is None
    pending.close()


@pytest.mark.integration
def test_pg_pending_count_is_pending_artists_plus_pending_songs(migrated_db: str) -> None:
    from backend.db.repositories.broadcast_artists import PgBroadcastArtistRepository
    from backend.db.repositories.broadcast_track_identities import (
        PgBroadcastTrackIdentityRepository,
    )
    from backend.db.repositories.pending_match_work import PgPendingMatchWork
    from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
    from backend.domain.enums import MatchStatus, MatchTier, ReasonCode

    with psycopg.connect(migrated_db, autocommit=True, row_factory=dict_row) as conn:
        artists = PgBroadcastArtistRepository(conn)
        songs = PgBroadcastTrackIdentityRepository(conn)

        def artist(name: str, status: MatchStatus) -> BroadcastArtist:
            a = artists.upsert(
                BroadcastArtist(id=uuid4(), original_name=name, normalized_name=name)
            )
            if status != MatchStatus.PENDING:
                artists.update_match_status(a.id, status, ReasonCode.LOW_CONFIDENCE, "r")
            return a

        def song(a: BroadcastArtist, title: str, status: MatchStatus) -> None:
            s = songs.upsert(
                BroadcastTrackIdentity(
                    id=uuid4(),
                    broadcast_artist_id=a.id,
                    original_title=title,
                    normalized_title=title,
                    normalized_signature=f"{a.normalized_name}:{title}",
                )
            )
            if status != MatchStatus.PENDING:
                songs.update_match_status(
                    s.id, status, MatchTier.LOCAL_FILE_FUZZY, ReasonCode.LOW_CONFIDENCE, "r"
                )

        abba = artist("abba", MatchStatus.PENDING)
        queen = artist("queen", MatchStatus.PENDING)
        artist("cher", MatchStatus.NEEDS_REVIEW)
        song(abba, "sos", MatchStatus.PENDING)
        song(abba, "waterloo", MatchStatus.PENDING)
        song(queen, "bohemian rhapsody", MatchStatus.PENDING)
        song(queen, "radio ga ga", MatchStatus.AUTO_MATCHED)
        song(queen, "innuendo", MatchStatus.MANUAL_REJECTED)

    pending = PgPendingMatchWork(
        lambda: psycopg.connect(migrated_db, autocommit=True, row_factory=dict_row)
    )
    try:
        assert pending.count_pending() == 5
    finally:
        pending.close()
