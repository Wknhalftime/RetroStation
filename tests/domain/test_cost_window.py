"""The meter's rolling cost window (PR G2 final review I2; not a locked test).

Requirements:
- I2: one 2 s sample is noisy, so the cost the suggestion divides by is the mean of the last
  ``COST_WINDOW_SAMPLES`` (15 samples, 30 s) measured costs;
- the window starts again when the cost's source changes (measured <-> reference), or when
  the number of measured engines changes materially: it doubles or halves against the count
  the window started with;
- D91, I4: a window never holds an invalid cost, and an empty one is the reference cost.
"""

from __future__ import annotations

import pytest

from backend.domain.stream_capacity import (
    COST_WINDOW_SAMPLES,
    REFERENCE_COST,
    CostSource,
    CostWindow,
    EngineCost,
    EngineReading,
    SessionCost,
    measured_cost,
)
from backend.domain.streaming import InvalidStreamValueError


def measured(cpu: float, memory: float = 75.0, engines: int = 1) -> EngineCost:
    return EngineCost(SessionCost(cpu, memory), CostSource.MEASURED, engines)


REFERENCE = EngineCost(REFERENCE_COST, CostSource.REFERENCE, 0)


def fill(window: CostWindow, *costs: EngineCost) -> CostWindow:
    for cost in costs:
        window = window.added(cost)
    return window


def test_the_window_is_thirty_seconds_of_samples() -> None:
    assert COST_WINDOW_SAMPLES == 15


def test_an_empty_window_is_the_reference_cost() -> None:
    assert CostWindow().mean() == REFERENCE


def test_the_mean_smooths_noisy_samples() -> None:
    window = fill(CostWindow(), measured(10.0, 70.0), measured(16.0, 80.0))
    mean = window.mean()
    assert (mean.cost.cpu_percent, mean.cost.memory_mb) == pytest.approx((13.0, 75.0))
    assert (mean.source, mean.engines) == (CostSource.MEASURED, 1)


def test_only_the_last_fifteen_samples_count() -> None:
    window = fill(CostWindow(), *[measured(50.0)] * 5, *[measured(10.0)] * COST_WINDOW_SAMPLES)
    assert len(window.costs) == COST_WINDOW_SAMPLES
    assert window.mean().cost.cpu_percent == pytest.approx(10.0)


def test_a_change_of_source_starts_the_window_again() -> None:
    window = fill(CostWindow(), measured(30.0), measured(30.0), REFERENCE)
    assert window.costs == (REFERENCE_COST,)
    assert window.mean() == REFERENCE
    window = window.added(measured(12.0))
    assert window.mean() == measured(12.0)


@pytest.mark.parametrize(
    ("start", "now", "restarts"),
    [(1, 2, True), (2, 3, False), (2, 4, True), (4, 3, False), (4, 2, True), (3, 5, False)],
    ids=["1->2", "2->3", "2->4", "4->3", "4->2", "3->5"],
)
def test_a_material_change_in_the_engines_starts_the_window_again(
    start: int, now: int, restarts: bool
) -> None:
    window = fill(CostWindow(), measured(20.0, engines=start), measured(20.0, engines=start))
    window = window.added(measured(10.0, engines=now))
    expected = 1 if restarts else 3
    assert len(window.costs) == expected
    assert window.mean().engines == (now if restarts else start)


def test_the_engine_count_is_anchored_where_the_window_started() -> None:
    # 2 -> 3 is not material, but a slow drift 2 -> 3 -> 4 has doubled since the start.
    window = fill(CostWindow(), measured(20.0, engines=2), measured(20.0, engines=3))
    assert len(window.costs) == 2
    window = window.added(measured(20.0, engines=4))
    assert len(window.costs) == 1


def test_measured_cost_counts_the_engines_it_averaged() -> None:
    readings = [EngineReading(12.0, 74.0), EngineReading(None, 70.0), EngineReading(14.0, 76.0)]
    assert measured_cost(readings).engines == 2
    assert measured_cost([]).engines == 0


def test_a_window_refuses_an_impossible_shape() -> None:
    with pytest.raises(InvalidStreamValueError):
        CostWindow(size=0)
    with pytest.raises(InvalidStreamValueError):
        CostWindow(costs=(REFERENCE_COST,) * 3, size=2)
