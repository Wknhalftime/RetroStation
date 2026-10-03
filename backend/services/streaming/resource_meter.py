"""The audio-engine cost meter: one progress row, rewritten every 2 s while streaming runs.

- D90: one fixed row (``task_id = task_type = "stream_resources"``), upserted in place,
  never a row per sample, and no log line per sample (a run of failed readings logs one
  warning, and one info line when the readings come back);
- D95: it counts the audio engines only (``scope: "audio_engines"``); D91: it carries the
  suggested listener limit; design note 15: the row's data;
- M17: each tick reads the meter and the machine, builds the row and writes it in a worker
  thread. The pids are read on the event loop first, because the stream service's sessions
  belong to the loop;
- PG13 and M9: stopping waits for the tick in flight (shielded), then completes the row;
  a row a crash left RUNNING is completed at the next start (``end_leftover_meter_row_with``);
- I4: a meter task that dies is logged once (``stream_meter_died``) and its row completed;
- I5: the row's times are UTC-aware, so the ``/ws`` reaper's ``now()`` never matches a
  fresh row; ``meter_row`` refuses a naive time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import structlog

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.stream_capacity import (
    DEFAULT_BUDGET,
    CapacityBudget,
    EngineCost,
    EngineReading,
    MachineTotals,
    Suggestion,
    measured_cost,
    suggest_max,
)
from backend.domain.streaming import InvalidStreamValueError
from backend.domain.system import TaskProgress
from backend.playout.process_meter import MachineUsage, ProcessUsage
from backend.repositories.task_progress import TaskProgressRepository

logger = structlog.get_logger()

SAMPLE_EVERY_S = 2.0
"""D90, PG9: the row is rewritten about every 2 s."""
METER_ID = TaskType.STREAM_RESOURCES.value
"""The one meter row's id: its task type (D90)."""
SCOPE = "audio_engines"
"""D95: what the cost covers."""

type Sleep = Callable[[float], Awaitable[None]]


class ResourceMeter(Protocol):
    """Reads processes and the machine (M6); ``PsutilMeter`` in production."""

    def read(self, pids: Sequence[int]) -> list[ProcessUsage]:
        """Each pid still running: its processor (None while warming) and memory."""
        ...

    def machine(self) -> MachineUsage:
        """The machine's threads, memory and processor load (None while warming)."""
        ...


class MeterRowWriter(Protocol):
    """Writes the meter's row and never raises for a lost database (``ProgressWriter``)."""

    def write(self, task: TaskProgress) -> None:
        """Upsert a RUNNING row; dropped once the row is completed (M9)."""
        ...

    def complete(self, task: TaskProgress) -> None:
        """Upsert the final row."""
        ...


@dataclass(frozen=True)
class MeterPorts:
    """What the meter reads and writes through (I5: ``clock`` is UTC-aware)."""

    pids: Callable[[], Sequence[int]]
    meter: ResourceMeter
    writer: MeterRowWriter
    clock: Callable[[], datetime]
    budget: CapacityBudget = field(default=DEFAULT_BUDGET)


@dataclass(frozen=True)
class MeterSample:
    """One reading of the engines and the machine, with the cost and the suggestion."""

    open_streams: int
    streams: Sequence[ProcessUsage]
    machine: MachineUsage
    cost: EngineCost
    suggestion: Suggestion
    budget: CapacityBudget


@dataclass(frozen=True)
class MeterRuntime:
    """A running meter: its task, its ports and when it started (the row's ``started_at``)."""

    task: asyncio.Task[None]
    ports: MeterPorts
    started_at: datetime


def sample_engines(
    pids: Sequence[int], meter: ResourceMeter, budget: CapacityBudget
) -> MeterSample:
    """Read the engines ``pids`` and the machine, then cost them (D91, D95). Every pid is an
    open stream (D42: every station's), whether or not it could be read."""
    streams = meter.read(pids)
    machine = meter.machine()
    cost = measured_cost([EngineReading(s.cpu_percent, s.memory_mb) for s in streams])
    totals = MachineTotals(threads=machine.threads, memory_mb=machine.memory_total_mb)
    return MeterSample(
        open_streams=len(pids),
        streams=streams,
        machine=machine,
        cost=cost,
        suggestion=suggest_max(cost.cost, totals, budget),
        budget=budget,
    )


def _row_data(sample: MeterSample) -> dict[str, object]:
    """Design note 15's keys, exactly; no ``processed`` or ``total``: a meter, not a task."""
    return {
        "open_streams": sample.open_streams,
        "scope": SCOPE,
        "streams": [
            {
                "cpu_percent": s.cpu_percent,
                "memory_mb": s.memory_mb,
                "warming": s.cpu_percent is None,
            }
            for s in sample.streams
        ],
        "cost": {
            "cpu_percent": sample.cost.cost.cpu_percent,
            "memory_mb": sample.cost.cost.memory_mb,
            "source": sample.cost.source.value,
        },
        "machine": {
            "threads": sample.machine.threads,
            "memory_total_mb": sample.machine.memory_total_mb,
            "memory_available_mb": sample.machine.memory_available_mb,
            "cpu_percent": sample.machine.cpu_percent,
        },
        "suggested_max": sample.suggestion.max_sessions,
        "limited_by": sample.suggestion.limited_by.value,
        "budget": {
            "cpu_share": sample.budget.cpu_share,
            "memory_share": sample.budget.memory_share,
        },
    }


def _require_aware(owner: str, **times: datetime) -> None:
    """I5: a naive time would read hours old (or new) to the reaper's ``now()``."""
    for name, value in times.items():
        if value.utcoffset() is None:
            raise InvalidStreamValueError(
                f"{owner}.{name} must be timezone-aware, got {value.isoformat()}"
            )


def meter_row(sample: MeterSample, started_at: datetime, now: datetime) -> TaskProgress:
    """The RUNNING meter row for ``sample``, stamped ``now`` (UTC-aware, I5)."""
    _require_aware("meter_row", started_at=started_at, now=now)
    return TaskProgress(
        task_id=METER_ID,
        task_type=TaskType.STREAM_RESOURCES,
        status=TaskStatus.RUNNING,
        progress_data=_row_data(sample),
        started_at=started_at,
        updated_at=now,
    )


def _ended_row(started_at: datetime, now: datetime) -> TaskProgress:
    """The COMPLETED meter row: nothing is measured any more."""
    _require_aware("ended meter row", now=now)
    return TaskProgress(
        task_id=METER_ID,
        task_type=TaskType.STREAM_RESOURCES,
        status=TaskStatus.COMPLETED,
        progress_data={"open_streams": 0, "scope": SCOPE},
        started_at=started_at,
        updated_at=now,
        completed_at=now,
    )


def _tick(ports: MeterPorts, pids: Sequence[int], started_at: datetime) -> None:
    """One sample, read and written (runs in a worker thread, M17)."""
    sample = sample_engines(pids, ports.meter, ports.budget)
    ports.writer.write(meter_row(sample, started_at, ports.clock()))


async def _land(tick: Awaitable[None]) -> None:
    """Await ``tick``. A cancel first lets the tick land (M9: its RUNNING write never comes
    after the completed row), then propagates."""
    in_flight = asyncio.ensure_future(tick)
    try:
        await asyncio.shield(in_flight)
    except asyncio.CancelledError:
        await asyncio.wait({in_flight})
        error = None if in_flight.cancelled() else in_flight.exception()
        if error is not None:  # the meter is stopping: a failed reading no longer matters
            log = logger.debug if isinstance(error, OSError) else logger.error
            log("stream_meter_last_tick_failed", error=repr(error))
        raise


async def run_meter(ports: MeterPorts, sleep: Sleep, started_at: datetime) -> None:
    """Sample every ``SAMPLE_EVERY_S`` until cancelled. A reading that fails with
    ``OSError`` skips its sample: the first of a run of failures logs a warning, the first
    sample after it one info line. Any other error ends the meter (I4)."""
    failing = False
    while True:
        pids = list(ports.pids())
        try:
            await _land(asyncio.to_thread(_tick, ports, pids, started_at))
        except OSError as error:
            if not failing:
                logger.warning("stream_meter_read_failed", error=str(error))
            failing = True
        else:
            if failing:
                logger.info("stream_meter_read_recovered")
            failing = False
        await sleep(SAMPLE_EVERY_S)


def _complete_in_background(writer: MeterRowWriter, row: TaskProgress) -> None:
    """Complete the row in a worker thread: the writer may wait on the database."""
    done = asyncio.get_running_loop().run_in_executor(None, writer.complete, row)
    done.add_done_callback(_report_unended)


def _report_unended(done: asyncio.Future[None]) -> None:
    """A completion that raised (the writer drops a lost database itself): logged."""
    if not done.cancelled() and done.exception() is not None:
        logger.error("stream_meter_row_unended", error=repr(done.exception()))


def _report_meter_end(
    ports: MeterPorts, started_at: datetime
) -> Callable[[asyncio.Task[None]], None]:
    """The meter's done-callback (I4): it only ends by cancellation (``stop_meter``, which
    completes the row itself); any other end is logged once and its row completed."""

    def report(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        logger.error(
            "stream_meter_died",
            message="the audio engine cost meter stopped",
            error=repr(error),
            exc_info=error,
        )
        _complete_in_background(ports.writer, _ended_row(started_at, ports.clock()))

    return report


def start_meter_task(ports: MeterPorts, sleep: Sleep) -> MeterRuntime:
    """Start sampling; its first row is written at once."""
    started_at = ports.clock()
    task = asyncio.create_task(run_meter(ports, sleep, started_at))
    task.add_done_callback(_report_meter_end(ports, started_at))
    return MeterRuntime(task, ports, started_at)


async def stop_meter(meter: MeterRuntime) -> None:
    """Stop sampling, wait for the tick in flight to land, then complete the row (PG13, M9)."""
    meter.task.cancel()
    await asyncio.wait({meter.task})
    ended = _ended_row(meter.started_at, meter.ports.clock())
    await asyncio.to_thread(meter.ports.writer.complete, ended)


def end_leftover_meter_row_with(repo: TaskProgressRepository, now: datetime) -> None:
    """Complete the meter row a crash left RUNNING, so the reaper never fails it (PG13); no
    row, or an ended one, is left alone."""
    row = repo.get_by_id(METER_ID)
    if row is None or row.status != TaskStatus.RUNNING:
        return
    repo.upsert(_ended_row(row.started_at, now))
