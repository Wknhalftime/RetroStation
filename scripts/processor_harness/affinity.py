"""The harness stays off the cores reserved for listener sessions (R18, Lance 2026-10-27).

Live sessions fell behind real time when harness jobs started: with four corpus runs side by
side, one session logged 11 catch-up warnings in 8 minutes, and none once it was pinned to the
last core with the harness on the others. Each harness command calls ``stay_off_stream_cores``
before any work; the renders, FFmpeg runs and worker processes it starts inherit the affinity.
"""

from __future__ import annotations

import psutil

from backend.config import Settings
from backend.playout.cpu_reservation import other_cpus, physical_cores


def stay_off_stream_cores(reserved_cores: int | None = None) -> tuple[int, ...]:
    """Move this process to the CPUs left by ``STREAM_RESERVED_CORES`` (or ``reserved_cores``)
    and return them."""
    count = Settings().stream_reserved_cores if reserved_cores is None else reserved_cores
    cpus = other_cpus(physical_cores(), count)
    psutil.Process().cpu_affinity(list(cpus))
    return cpus
