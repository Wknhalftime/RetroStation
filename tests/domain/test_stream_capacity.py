"""The suggested listener limit and the engine cost (PR G2, Task 8; traceability P: T8.1-T8.6).

Requirements:
- D91: "The suggested maximum listeners lets streams use half the CPU and a quarter of the RAM
  (about 57 on the 16-thread, 64 GB dev PC). Disk speed is not counted";
  design note 14: ``max(1, min(floor(0.5 x threads x 100 / cpu %), floor(0.25 x memory /
  memory per stream)))``, and the limiting resource is named;
- D27: a limit is a whole number of 1 or more, so the suggestion never goes below 1;
- the PR A engine gate: 13.1-13.8 % of a core and 74-75 MB per session; the reference cost
  is its upper end, used when no engine can be measured;
- I4 and M7: an engine reading under ``CPU_FLOOR`` (1 %) counts as warming, so a 0 % reading
  never divides by zero; with every engine warming the cost is the reference;
- H4: the values validate, naming the field (a cost must be above 0 % CPU).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from backend.domain.stream_capacity import (
    CPU_FLOOR,
    REFERENCE_COST,
    CapacityBudget,
    CostSource,
    EngineReading,
    LimitedBy,
    MachineTotals,
    SessionCost,
    Suggestion,
    measured_cost,
    suggest_max,
)
from backend.domain.streaming import InvalidStreamValueError

DEV_PC = MachineTotals(threads=16, memory_mb=65_430)


@pytest.mark.parametrize(
    ("cost", "machine", "expected"),
    [
        (
            SessionCost(cpu_percent=10.0, memory_mb=100.0),
            MachineTotals(threads=4, memory_mb=4_096),
            Suggestion(max_sessions=10, limited_by=LimitedBy.MEMORY),
        ),
        (
            SessionCost(cpu_percent=20.0, memory_mb=80.0),
            DEV_PC,
            Suggestion(max_sessions=40, limited_by=LimitedBy.PROCESSOR),
        ),
    ],
    ids=["memory-limits", "processor-limits"],
)
def test_the_suggestion_is_the_smaller_of_processor_and_memory_room(
    cost: SessionCost, machine: MachineTotals, expected: Suggestion
) -> None:
    # T8.1 (D91): processor room 0.5 x 4 x 100 / 10 = 20 against memory room 0.25 x 4096 /
    # 100 = 10.24; then 0.5 x 16 x 100 / 20 = 40 against 0.25 x 65430 / 80 = 204.
    assert suggest_max(cost, machine) == expected


def test_the_worked_example_for_this_machine() -> None:
    # T8.2 (D91: "about 57 on the 16-thread, 64 GB dev PC"): 0.5 x 16 x 100 / 13.8 = 57.97.
    assert suggest_max(REFERENCE_COST, DEV_PC) == Suggestion(57, LimitedBy.PROCESSOR)
    assert suggest_max(REFERENCE_COST, DEV_PC, CapacityBudget()) == Suggestion(
        57, LimitedBy.PROCESSOR
    )


def test_the_suggestion_is_never_below_1() -> None:
    # T8.3 (D91, D27): an engine heavier than the whole budget still allows one listener.
    heavy = SessionCost(cpu_percent=400.0, memory_mb=75.0)
    assert suggest_max(heavy, MachineTotals(threads=2, memory_mb=65_430)) == Suggestion(
        1, LimitedBy.PROCESSOR
    )
    huge = SessionCost(cpu_percent=1.0, memory_mb=10_000.0)
    assert suggest_max(huge, MachineTotals(threads=16, memory_mb=8_192)).max_sessions == 1


@pytest.mark.parametrize(
    ("build", "field"),
    [
        (lambda: MachineTotals(threads=0, memory_mb=65_430), "MachineTotals.threads"),
        (lambda: MachineTotals(threads=16, memory_mb=0), "MachineTotals.memory_mb"),
        (lambda: SessionCost(cpu_percent=0.0, memory_mb=75.0), "SessionCost.cpu_percent"),
        (lambda: SessionCost(cpu_percent=-1.0, memory_mb=75.0), "SessionCost.cpu_percent"),
        (lambda: SessionCost(cpu_percent=13.8, memory_mb=0.0), "SessionCost.memory_mb"),
        (lambda: CapacityBudget(cpu_share=1.5, memory_share=0.25), "CapacityBudget.cpu_share"),
    ],
    ids=["no-threads", "no-memory", "cpu-0", "cpu-negative", "no-memory-per-stream", "share"],
)
def test_capacity_values_refuse_impossible_numbers_naming_the_field(
    build: Callable[[], object], field: str
) -> None:
    # T8.4 (H4; I4: a cost of 0 % CPU cannot exist, so it can never be divided by).
    with pytest.raises(InvalidStreamValueError, match=field):
        build()


def test_the_reference_cost_is_the_engine_gates() -> None:
    # T8.5 (the PR A gate's upper end), guard: and the default budget is D91's half and quarter.
    assert SessionCost(cpu_percent=13.8, memory_mb=75.0) == REFERENCE_COST
    assert CapacityBudget() == CapacityBudget(cpu_share=0.5, memory_share=0.25)


@pytest.mark.parametrize(
    ("readings", "cost", "source"),
    [
        (
            [EngineReading(cpu_percent=0.0, memory_mb=74.0), EngineReading(12.0, 74.0)],
            SessionCost(cpu_percent=12.0, memory_mb=74.0),
            CostSource.MEASURED,
        ),
        (
            [EngineReading(cpu_percent=0.0, memory_mb=70.0), EngineReading(0.99, 71.0)],
            REFERENCE_COST,
            CostSource.REFERENCE,
        ),
        (
            [EngineReading(cpu_percent=None, memory_mb=70.0)],
            REFERENCE_COST,
            CostSource.REFERENCE,
        ),
        ([], REFERENCE_COST, CostSource.REFERENCE),
    ],
    ids=["one-idle-one-measured", "all-under-the-floor", "warming", "no-engine"],
)
def test_engines_under_1_percent_count_as_warming_and_all_warming_means_reference(
    readings: list[EngineReading], cost: SessionCost, source: CostSource
) -> None:
    # T8.6 (I4, M7): no division by zero, and no 0 % engine pulling the processor mean down.
    # How a warming engine's memory is counted is not a requirement (audit SF8): the first
    # case gives both engines the same memory.
    assert CPU_FLOOR == 1.0
    measured = measured_cost(readings)
    assert (measured.cost, measured.source) == (cost, source)
    assert suggest_max(measured.cost, DEV_PC).max_sessions >= 1
