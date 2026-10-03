"""Stream capacity: what one stream's audio engine costs, and the suggested listener limit.

D91: streams may use half the processor threads and a quarter of the memory; disk speed is
not counted. A limit above the suggestion is allowed (the page warns). D95: the cost is the
audio engines' only; the app's own share is not estimated. I4: an engine reading under
``CPU_FLOOR`` is still warming, so a 0 % reading is never divided by.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from backend.domain.streaming import InvalidStreamValueError

CPU_FLOOR = 1.0
"""An engine reading under 1 % of a thread counts as warming (I4, M7)."""


def _require_positive(owner: str, **fields: float) -> None:
    for name, value in fields.items():
        if not value > 0:  # also refuses NaN
            raise InvalidStreamValueError(f"{owner}.{name} must be > 0, got {value}")


@dataclass(frozen=True)
class SessionCost:
    """One stream's audio engine: % of one thread, and MB of memory. Never 0 % (I4)."""

    cpu_percent: float
    memory_mb: float

    def __post_init__(self) -> None:
        _require_positive("SessionCost", cpu_percent=self.cpu_percent, memory_mb=self.memory_mb)


REFERENCE_COST = SessionCost(cpu_percent=13.8, memory_mb=75.0)
"""The PR A engine gate's upper end (13.1-13.8 % of a core, 74-75 MB per session): the cost
used while no engine can be measured."""


@dataclass(frozen=True)
class MachineTotals:
    """What the suggestion divides: the processor threads and the memory, in MB."""

    threads: int
    memory_mb: float

    def __post_init__(self) -> None:
        _require_positive("MachineTotals", threads=self.threads, memory_mb=self.memory_mb)


@dataclass(frozen=True)
class CapacityBudget:
    """The share of the machine streams may use (D91: half the threads, a quarter of the
    memory)."""

    cpu_share: float = 0.5
    memory_share: float = 0.25

    def __post_init__(self) -> None:
        for name, share in (("cpu_share", self.cpu_share), ("memory_share", self.memory_share)):
            if not 0 < share <= 1:
                raise InvalidStreamValueError(
                    f"CapacityBudget.{name} must be in (0, 1], got {share}"
                )


DEFAULT_BUDGET = CapacityBudget()
"""D91's budget: half the threads and a quarter of the memory."""


class LimitedBy(StrEnum):
    """The resource that runs out first."""

    PROCESSOR = "processor"
    MEMORY = "memory"


@dataclass(frozen=True)
class Suggestion:
    """The suggested listener limit (D91), never below 1 (D27), and what limits it."""

    max_sessions: int
    limited_by: LimitedBy

    def __post_init__(self) -> None:
        if self.max_sessions < 1:
            raise InvalidStreamValueError(
                f"Suggestion.max_sessions must be >= 1, got {self.max_sessions}"
            )


def suggest_max(
    cost: SessionCost, machine: MachineTotals, budget: CapacityBudget = DEFAULT_BUDGET
) -> Suggestion:
    """D91 (design note 14): the smaller of the processor and the memory room, never below
    1; a tie is the processor's."""
    by_cpu = math.floor(budget.cpu_share * machine.threads * 100 / cost.cpu_percent)
    by_memory = math.floor(budget.memory_share * machine.memory_mb / cost.memory_mb)
    if by_cpu <= by_memory:
        return Suggestion(max(1, by_cpu), LimitedBy.PROCESSOR)
    return Suggestion(max(1, by_memory), LimitedBy.MEMORY)


class CostSource(StrEnum):
    """Where a cost came from: the engines measured, or the PR A gate's reference."""

    MEASURED = "measured"
    REFERENCE = "reference"


@dataclass(frozen=True)
class EngineReading:
    """One engine's reading: its processor (% of one thread; None while warming) and its
    memory in MB."""

    cpu_percent: float | None
    memory_mb: float

    def __post_init__(self) -> None:
        if self.cpu_percent is not None and not self.cpu_percent >= 0:
            raise InvalidStreamValueError(
                f"EngineReading.cpu_percent must be >= 0 or None, got {self.cpu_percent}"
            )
        if not self.memory_mb >= 0:
            raise InvalidStreamValueError(
                f"EngineReading.memory_mb must be >= 0, got {self.memory_mb}"
            )


@dataclass(frozen=True)
class EngineCost:
    """One stream's cost and where it came from."""

    cost: SessionCost
    source: CostSource


def measured_cost(readings: Sequence[EngineReading]) -> EngineCost:
    """The mean of the engines measured at or above ``CPU_FLOOR``; with none, the reference
    (I4: no division by zero, and no warming engine pulling the processor mean down)."""
    usable = [
        (reading.cpu_percent, reading.memory_mb)
        for reading in readings
        if reading.cpu_percent is not None
        and reading.cpu_percent >= CPU_FLOOR
        and reading.memory_mb > 0
    ]
    if not usable:
        return EngineCost(REFERENCE_COST, CostSource.REFERENCE)
    cpu = sum(cpu for cpu, _ in usable) / len(usable)
    memory = sum(memory for _, memory in usable) / len(usable)
    return EngineCost(SessionCost(cpu, memory), CostSource.MEASURED)
