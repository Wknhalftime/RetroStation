"""D91's listener limit with reserved cores (R18, Lance 2026-10-27): sessions run on reserved
cores, about 4 listeners a core at the 50% p99.9 frame-load mark (on the 8-core desktop, 30
sessions passed and 40 failed), so the processor room is 4 listeners a reserved core instead of
half the threads."""

from __future__ import annotations

import pytest

from backend.domain.stream_capacity import (
    LISTENERS_PER_RESERVED_CORE,
    REFERENCE_COST,
    CapacityBudget,
    LimitedBy,
    MachineTotals,
    Suggestion,
    suggest_max,
)
from backend.domain.streaming import InvalidStreamValueError

ROOMY = MachineTotals(threads=16, memory_mb=32768)


def test_four_listeners_a_reserved_core() -> None:
    assert LISTENERS_PER_RESERVED_CORE == 4


@pytest.mark.parametrize(("cores", "listeners"), [(1, 4), (2, 8), (3, 12)])
def test_with_reserved_cores_the_processor_room_follows_them(cores: int, listeners: int) -> None:
    suggestion = suggest_max(REFERENCE_COST, ROOMY, CapacityBudget(reserved_cores=cores))

    assert suggestion == Suggestion(listeners, LimitedBy.PROCESSOR)


def test_with_reserved_cores_memory_can_still_be_the_limit() -> None:
    budget = CapacityBudget(reserved_cores=2)

    suggestion = suggest_max(REFERENCE_COST, MachineTotals(threads=16, memory_mb=1500), budget)

    assert suggestion == Suggestion(5, LimitedBy.MEMORY)  # a quarter of 1500 MB at 75 MB


def test_without_reserved_cores_the_room_is_still_half_the_threads() -> None:
    suggestion = suggest_max(REFERENCE_COST, ROOMY, CapacityBudget(reserved_cores=0))

    assert suggestion == Suggestion(57, LimitedBy.PROCESSOR)  # 0.5 * 16 threads / 13.8 %


def test_reserved_cores_cannot_be_negative() -> None:
    with pytest.raises(InvalidStreamValueError, match="reserved_cores"):
        CapacityBudget(reserved_cores=-1)
