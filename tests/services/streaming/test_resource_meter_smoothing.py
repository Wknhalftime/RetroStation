"""The meter row's cost and suggestion over noisy samples (PR G2 final review I2; not locked).

Requirements:
- I2: one 2 s sample is noisy, so the row's cost, and the suggested limit computed from it
  (D91), is a rolling mean of the measured cost over 15 samples (30 s); the page's "Use
  suggested (N)" label and its above-suggestion warning then hold still while the engines'
  load does not really change;
- each engine's own reading stays the raw sample (design note 15: ``streams``);
- the mean starts again when the cost's source changes (measured <-> reference).

No sleeps: the sampler's ``sleep`` is the locked rig's ``GatedSleep``, released tick by tick.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from backend.domain.system import TaskProgress
from backend.playout.process_meter import MachineUsage, ProcessUsage
from backend.services.streaming.resource_meter import (
    MeterPorts,
    ResourceMeter,
    start_meter_task,
    stop_meter,
)
from tests.services.streaming.events_rig import GatedSleep
from tests.services.streaming.helpers import Clock

T0 = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
MACHINE = MachineUsage(
    threads=16, memory_total_mb=65_430.0, memory_available_mb=40_100.0, cpu_percent=12.0
)
HANG_GUARD_S = 10.0
"""Fails a test whose worker-thread tick never lands; a passing run never waits on it."""


async def until(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + HANG_GUARD_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the condition was not reached")
        await asyncio.sleep(0)


@dataclass
class ScriptedMeter:
    """A ``ResourceMeter`` whose one engine reads the next scripted cpu % on each tick
    (``None``: no engine is running on that tick)."""

    script: list[float | None]
    calls: int = 0

    def read(self, pids: Sequence[int]) -> list[ProcessUsage]:
        cpu = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return [] if cpu is None else [ProcessUsage(pid, cpu, 75.0) for pid in pids]

    def machine(self) -> MachineUsage:
        return MACHINE


@dataclass
class Rows:
    rows: list[TaskProgress] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def write(self, task: TaskProgress) -> None:
        with self._lock:
            self.rows.append(task)

    def complete(self, task: TaskProgress) -> None:
        self.write(task)


async def run_ticks(script: list[float | None]) -> list[TaskProgress]:
    """Run one tick per scripted reading and return the RUNNING rows, in order."""
    clock = Clock(T0)
    sleep = GatedSleep(clock)
    writer = Rows()
    meter: ResourceMeter = ScriptedMeter(script)
    ports = MeterPorts(pids=lambda: [101], meter=meter, writer=writer, clock=clock)
    runtime = start_meter_task(ports, sleep)
    for done in range(1, len(script) + 1):
        await until(lambda done=done: len(sleep.asked) == done and sleep.pending == 1)
        if done < len(script):
            sleep.release()
    await stop_meter(runtime)
    return [row for row in writer.rows if row.status == "running"]


def suggestions(rows: list[TaskProgress]) -> list[int]:
    return [row.progress_data["suggested_max"] for row in rows]


async def test_noisy_samples_give_a_stable_suggestion() -> None:
    # 10 % and 16 % in turn (a mean of 13 %): one sample alone suggests 80, then 50, then 80;
    # 0.5 x 16 x 100 / 13 = 61.5.
    rows = await run_ticks([10.0, 16.0] * 10)
    assert len(rows) == 20
    assert suggestions(rows)[:2] == [80, 61]
    # From the third sample on, the suggestion stays within a few listeners of 61.5.
    assert all(60 <= value <= 66 for value in suggestions(rows)[2:])
    # Once the window is full (15 samples: 8 at one level and 7 at the other) it moves by at
    # most two listeners a tick, never 30.
    assert set(suggestions(rows)[14:]) <= {60, 61, 62}
    # Each engine's reading stays the raw sample.
    assert [row.progress_data["streams"][0]["cpu_percent"] for row in rows[:2]] == [10.0, 16.0]


async def test_the_mean_starts_again_when_the_source_changes() -> None:
    # Measured at 40 %, nobody listening (the reference), then measured at 10 %: the 40 %
    # samples no longer count.
    rows = await run_ticks([40.0, 40.0, None, 10.0])
    costs = [row.progress_data["cost"] for row in rows]
    assert [cost["source"] for cost in costs] == ["measured", "measured", "reference", "measured"]
    assert costs[1]["cpu_percent"] == 40.0
    assert costs[2]["cpu_percent"] == 13.8
    assert costs[3]["cpu_percent"] == 10.0
    assert suggestions(rows)[3] == 80
