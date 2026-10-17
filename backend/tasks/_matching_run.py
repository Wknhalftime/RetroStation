"""The matching run's progress row, as the re-check and the matching stages report to it.

Spec 2026-10-17 "Matching run progress": a Re-run Matching (or an MB pass's re-check) is one
run. The re-check opens one RUNNING ``matching`` row and queues ``artist_matching_task(pid,
run_id)`` per playlist; each artist stage hands off ``identity_matching_task(pid, run_id)``.
The row counts its stages: P at the open, an artist stage that hands off is replaced by its
identity stage, and every other way a stage ends finishes it (failed unless an identity stage
succeeded). The last stage closes the row.

- ``run_ports`` is the one place the ports are built, on connections of their own
  (autocommit, bounded statements): never a stage's transactional connection.
- ``matching_stage`` wraps one stage: it counts the pending work at the start, gives the
  matching service a throttled ``progress`` observer, and finishes the stage on the way out.
- ``run_fan_out`` wraps the re-check: it opens the row and accounts for every stage it was
  meant to queue, so a re-check that fails after opening closes the row itself.

Everything here is telemetry: the ports never raise for a database error, so matching never
stops on a progress write. With no run id a stage touches none of it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg

from backend.db.progress_writer import writer_options
from backend.db.repositories.matching_run import PgMatchingRunRepository
from backend.db.repositories.pending_match_work import PgPendingMatchWork
from backend.db.sync_conn import connect_sync
from backend.repositories.matching_run import MatchingRunRepository
from backend.repositories.pending_match_work import PendingMatchWork
from backend.services.matching_progress import NO_PROGRESS, MatchingProgress

REFRESH_INTERVAL_S = 2.0
"""A stage writes the row at most once per this many seconds of its clock (the start's write
counts as one)."""


@dataclass(frozen=True)
class RunPorts:
    runs: MatchingRunRepository
    pending: PendingMatchWork
    clock: Callable[[], float]
    """Monotonic seconds."""


@contextmanager
def run_ports(database_url: str) -> Iterator[RunPorts]:
    """The run row and the pending count, each on its own autocommit connection with bounded
    statements (``writer_options()``), opened when first used and closed at the end."""

    def connect() -> psycopg.Connection[Any]:
        return connect_sync(database_url, autocommit=True, options=writer_options())

    runs = PgMatchingRunRepository(connect)
    pending = PgPendingMatchWork(connect)
    try:
        yield RunPorts(runs=runs, pending=pending, clock=time.monotonic)
    finally:
        runs.close()
        pending.close()


# --- one stage -------------------------------------------------------------------------------


class _StageProgress:
    """A stage's ``MatchingProgress``: ``pending = start count - ticks``, written at most once
    per ``REFRESH_INTERVAL_S``. A start count that failed means no writes at all."""

    def __init__(self, run_id: str, ports: RunPorts, start_count: int | None) -> None:
        self._run_id = run_id
        self._ports = ports
        self._start_count = start_count
        self._settled = 0
        self._last_write = ports.clock()

    def tick(self) -> None:
        self._settled += 1
        estimate = self.estimate()
        if estimate is None:
            return
        now = self._ports.clock()
        if now - self._last_write < REFRESH_INTERVAL_S:
            return
        self._last_write = now
        self._ports.runs.refresh(self._run_id, pending=estimate)

    def estimate(self) -> int | None:
        if self._start_count is None:
            return None
        return max(0, self._start_count - self._settled)


class Stage:
    """What a task tells its stage: the observer for its matching service, and how it ended
    when it did not simply succeed or raise."""

    def __init__(self, progress: MatchingProgress) -> None:
        self.progress = progress
        self.handed_on = False
        self.refused = False

    def hand_on(self) -> None:
        """The stage's work passes to the next stage (its hand-off was queued)."""
        self.handed_on = True

    def refuse(self) -> None:
        """The stage's hand-off was refused: it ends here, failed."""
        self.refused = True


@contextmanager
def matching_stage(run_id: str | None, database_url: str) -> Iterator[Stage]:
    """One matching stage of run ``run_id`` (None: no run, nothing reported).

    On the way in: count the pending work and write it at once. On the way out, unless the
    stage handed on: finish it, failed when it raised or its hand-off was refused. The count
    given is a fresh one, else ``start count - ticks``, else None.
    """
    if run_id is None:
        yield Stage(NO_PROGRESS)
        return
    with run_ports(database_url) as ports:
        start_count = ports.pending.count_pending()
        if start_count is not None:
            ports.runs.refresh(run_id, pending=start_count)
        progress = _StageProgress(run_id, ports, start_count)
        stage = Stage(progress)
        raised = True
        try:
            yield stage
            raised = False
        finally:
            if raised or not stage.handed_on:
                fresh = ports.pending.count_pending()
                ports.runs.finish_stage(
                    run_id,
                    pending=fresh if fresh is not None else progress.estimate(),
                    failed=raised or stage.refused,
                )


# --- the re-check's fan-out ---------------------------------------------------------------------


class RunFanOut:
    """The re-check's side of the run: open the row, then account for each stage queued."""

    def __init__(self, ports: RunPorts) -> None:
        self._ports = ports
        self.run_id: str | None = None
        self._stages = 0
        self._accounted = 0

    def open(self, stages: int) -> None:
        """Open the run row for ``stages`` playlists, counted now. No stage, no run."""
        if stages <= 0:
            return
        self.run_id = uuid.uuid4().hex
        self._stages = stages
        total = self._ports.pending.count_pending()
        self._ports.runs.open(self.run_id, total=total or 0, stages=stages)

    def refused(self) -> None:
        """A stage's enqueue was refused: it ends now, failed."""
        self._finish_failed()

    def accounted(self) -> None:
        """One stage's enqueue was tried (queued, or refused and finished)."""
        self._accounted += 1

    def close_unqueued(self) -> None:
        """Every stage never tried ends now, failed: the re-check stopped before queuing it."""
        while self._accounted < self._stages:
            self._accounted += 1
            self._finish_failed()

    def _finish_failed(self) -> None:
        if self.run_id is not None:
            self._ports.runs.finish_stage(
                self.run_id, pending=self._ports.pending.count_pending(), failed=True
            )


@contextmanager
def run_fan_out(database_url: str) -> Iterator[RunFanOut]:
    """The re-check's run. Whatever way the block ends, a stage it opened but never queued
    is finished as failed, so the row closes instead of waiting for the ``/ws`` reaper."""
    with run_ports(database_url) as ports:
        fan_out = RunFanOut(ports)
        try:
            yield fan_out
        finally:
            fan_out.close_unqueued()
