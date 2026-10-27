"""Processor cores kept for listener sessions (R18, Lance 2026-10-27).

Live sessions fell behind real time while harness jobs ran, with the machine mostly idle: a
scheduling problem. Sessions now run on the last ``STREAM_RESERVED_CORES`` physical cores at
above-normal priority, and the harness on the others. A physical core's logical processors (its
hyperthreads) always go together.

Takes primitives only, like the rest of ``backend.playout``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

__all__ = [
    "CoreReservationError",
    "affinity_mask",
    "other_cpus",
    "physical_cores",
    "reserved_cpus",
]

type Cores = Sequence[Sequence[int]]
"""Each physical core's logical processor numbers, in the order Windows lists them."""


class CoreReservationError(ValueError):
    """The reservation leaves no core for everything else, or is negative."""


def reserved_cpus(cores: Cores, count: int) -> tuple[int, ...]:
    """The logical processors of the last ``count`` physical cores."""
    if not 0 <= count < len(cores):
        raise CoreReservationError(
            f"STREAM_RESERVED_CORES (.env): must be 0 to {len(cores) - 1} on this machine's "
            f"{len(cores)} cores (one is left for everything else), got {count}"
        )
    return tuple(cpu for core in cores[len(cores) - count :] for cpu in core)


def other_cpus(cores: Cores, count: int) -> tuple[int, ...]:
    """The logical processors the reservation leaves for everything else."""
    reserved = set(reserved_cpus(cores, count))
    return tuple(cpu for core in cores for cpu in core if cpu not in reserved)


def affinity_mask(cpus: Sequence[int]) -> int:
    """The Windows affinity mask with one bit per logical processor."""
    mask = 0
    for cpu in cpus:
        mask |= 1 << cpu
    return mask


if sys.platform == "win32":  # an if-statement, so mypy --platform linux skips the body
    import ctypes
    from ctypes import wintypes

    class _ProcessorInfo(ctypes.Structure):  # SYSTEM_LOGICAL_PROCESSOR_INFORMATION
        _fields_ = [
            ("ProcessorMask", ctypes.c_size_t),
            ("Relationship", ctypes.c_int),
            ("_union", ctypes.c_ubyte * 16),
        ]

    _RELATION_PROCESSOR_CORE = 0

    def physical_cores() -> list[tuple[int, ...]]:
        """This machine's physical cores, each as its logical processors."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        size = wintypes.DWORD(0)
        kernel32.GetLogicalProcessorInformation(None, ctypes.byref(size))
        entries = (_ProcessorInfo * (size.value // ctypes.sizeof(_ProcessorInfo)))()
        if not kernel32.GetLogicalProcessorInformation(entries, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        masks = [e.ProcessorMask for e in entries if e.Relationship == _RELATION_PROCESSOR_CORE]
        return sorted(tuple(cpu for cpu in range(64) if mask >> cpu & 1) for mask in masks)

else:

    def physical_cores() -> list[tuple[int, ...]]:
        """Off Windows, where streaming does not run (D5), each logical processor is a core."""
        return [(cpu,) for cpu in range(os.cpu_count() or 1)]
