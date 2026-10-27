"""One setting, STREAM_RESERVED_CORES, decides both the cores listener sessions are pinned to and
D91's listener limit (R18, Lance 2026-10-27: 2 cores, about 8 listeners against a peak of 3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import backend.main as main_module
from backend.config import Settings
from backend.domain.stream_capacity import (
    LISTENERS_PER_RESERVED_CORE,
    REFERENCE_COST,
    MachineTotals,
    suggest_max,
)

DESKTOP = [(0, 1), (2, 3), (4, 5), (6, 7), (8, 9), (10, 11), (12, 13), (14, 15)]
ROOMY = MachineTotals(threads=16, memory_mb=32768)


def test_two_cores_are_reserved_by_default() -> None:
    assert Settings().stream_reserved_cores == 2


def test_a_negative_reservation_is_refused_naming_the_setting() -> None:
    with pytest.raises(ValidationError, match="STREAM_RESERVED_CORES"):
        Settings(stream_reserved_cores=-1)


def test_sessions_are_pinned_to_the_last_reserved_cores() -> None:
    settings = Settings(stream_reserved_cores=2)

    assert main_module.stream_cpus(settings, DESKTOP) == (12, 13, 14, 15)


@pytest.mark.parametrize("cores", [0, 1, 2, 3])
def test_the_listener_limit_and_the_pinned_cores_come_from_one_setting(cores: int) -> None:
    settings = Settings(stream_reserved_cores=cores)

    pinned = main_module.stream_cpus(settings, DESKTOP)
    suggestion = suggest_max(REFERENCE_COST, ROOMY, main_module.stream_budget(settings))

    assert len(pinned) == 2 * cores
    if cores:
        assert suggestion.max_sessions == LISTENERS_PER_RESERVED_CORE * len(pinned) // 2
