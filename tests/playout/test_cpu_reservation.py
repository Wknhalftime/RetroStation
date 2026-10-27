"""Processor cores kept for listener sessions (R18, Lance 2026-10-27: STREAM_RESERVED_CORES = 2).

Sessions run on the last physical cores, everything the harness starts on the others; a core's
logical processors (its hyperthreads) always go together.
"""

from __future__ import annotations

import psutil
import pytest

from backend.playout.cpu_reservation import (
    CoreReservationError,
    affinity_mask,
    other_cpus,
    physical_cores,
    reserved_cpus,
)

CORES = [(0, 1), (2, 3), (4, 5), (6, 7)]


def test_the_reserved_cores_are_the_last_ones_with_both_their_threads() -> None:
    assert reserved_cpus(CORES, 2) == (4, 5, 6, 7)


def test_the_other_cpus_are_the_rest() -> None:
    assert other_cpus(CORES, 2) == (0, 1, 2, 3)


def test_no_reserved_cores_reserves_nothing_and_leaves_every_cpu() -> None:
    assert reserved_cpus(CORES, 0) == ()
    assert other_cpus(CORES, 0) == (0, 1, 2, 3, 4, 5, 6, 7)


@pytest.mark.parametrize("count", [4, 5, -1])
def test_a_reservation_that_leaves_no_core_for_the_rest_is_refused(count: int) -> None:
    with pytest.raises(CoreReservationError, match="STREAM_RESERVED_CORES"):
        reserved_cpus(CORES, count)


def test_the_affinity_mask_has_one_bit_per_cpu() -> None:
    assert affinity_mask((14, 15)) == 0xC000
    assert affinity_mask(()) == 0


def test_this_machines_cores_cover_every_logical_cpu_once() -> None:
    cpus = [cpu for core in physical_cores() for cpu in core]

    assert sorted(cpus) == list(range(psutil.cpu_count() or 0))
