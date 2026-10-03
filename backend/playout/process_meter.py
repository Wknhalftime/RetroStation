"""The audio engines' processor and memory, and the machine's, read with psutil.

D90, D95: each engine's processor (% of one thread) and memory (MB), by process; the machine's
threads, memory and processor load. PG10: psutil is imported here only; the services see
this meter through their ``ResourceMeter`` Protocol (M6).

M7:
- psutil's first ``cpu_percent`` of a process is meaningless (0.0), so a process is
  "warming" (None) until its second reading; the machine's first load reading too;
- a pid the meter was not asked for is forgotten, so a reused pid is never read as the old
  process;
- an engine's children (``children(recursive=True)``) are added to it, in case one ever
  forks.

Error handling: a process that has exited, or that cannot be read, is left out, never raised.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field

import psutil

_MB = 1024 * 1024
_GONE = (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied)
"""A process that exited (or is exiting), or that this user may not read: it is left out."""


@dataclass(frozen=True)
class ProcessUsage:
    """One engine: its pid, processor (% of one thread; None while warming) and memory."""

    pid: int
    cpu_percent: float | None
    memory_mb: float


@dataclass(frozen=True)
class MachineUsage:
    """The machine: logical threads, total and available memory, and its processor load
    (% of the whole machine; None on the first reading)."""

    threads: int
    memory_total_mb: float
    memory_available_mb: float
    cpu_percent: float | None


@dataclass
class _Tracked:
    """An engine read before, and its children read before (each keeps its own processor
    baseline, so a child's figure is meaningful from its second reading)."""

    process: psutil.Process
    children: dict[int, psutil.Process] = field(default_factory=dict)


class PsutilMeter:
    """A ``ResourceMeter`` over psutil. Not thread-safe: one sampler calls it at a time."""

    def __init__(self) -> None:
        self._tracked: dict[int, _Tracked] = {}
        self._machine_primed = False

    def read(self, pids: Sequence[int]) -> list[ProcessUsage]:
        """Each engine in ``pids`` still running, in order; the others are forgotten."""
        asked = set(pids)
        for forgotten in [pid for pid in self._tracked if pid not in asked]:
            del self._tracked[forgotten]
        found = (self._read_one(pid) for pid in pids)
        return [usage for usage in found if usage is not None]

    def machine(self) -> MachineUsage:
        """The machine's totals; its first processor reading is warming (None)."""
        cpu = psutil.cpu_percent(None)
        warming, self._machine_primed = not self._machine_primed, True
        memory = psutil.virtual_memory()
        return MachineUsage(
            threads=psutil.cpu_count(logical=True) or os.cpu_count() or 1,
            memory_total_mb=memory.total / _MB,
            memory_available_mb=memory.available / _MB,
            cpu_percent=None if warming else cpu,
        )

    def _read_one(self, pid: int) -> ProcessUsage | None:
        known = self._tracked.get(pid)
        try:
            tracked = known if known is not None else _Tracked(psutil.Process(pid))
            cpu = tracked.process.cpu_percent(None)
            memory = float(tracked.process.memory_info().rss)
            children = tracked.process.children(recursive=True)
        except _GONE:
            self._tracked.pop(pid, None)
            return None
        child_cpu, child_memory = _read_children(tracked, children)
        self._tracked[pid] = tracked
        warming = known is None
        return ProcessUsage(
            pid, None if warming else cpu + child_cpu, (memory + child_memory) / _MB
        )


def _read_children(tracked: _Tracked, children: list[psutil.Process]) -> tuple[float, float]:
    """The children's processor (those read before) and memory (all); a child that exited
    is left out, and ``tracked`` keeps only the children still running."""
    cpu = memory = 0.0
    running: dict[int, psutil.Process] = {}
    for child in children:
        process = tracked.children.get(child.pid, child)
        try:
            child_cpu = process.cpu_percent(None)
            memory += process.memory_info().rss
        except _GONE:
            continue
        if child.pid in tracked.children:
            cpu += child_cpu
        running[child.pid] = process
    tracked.children = running
    return cpu, memory
