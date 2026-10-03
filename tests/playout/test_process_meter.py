"""The psutil process meter (PR G2, Task 8; traceability Q: T8.7-T8.10).

Requirements:
- D90 and D95: the meter reads each audio engine's processor (% of one thread) and memory, by
  process, and the machine's totals (threads, memory, its processor load);
- D5/D6: one engine process per stream; the meter is given their pids;
- M7: a process's first processor reading is meaningless (psutil answers 0.0), so it is
  "warming" (None) until its second reading; the machine's first reading too; a pid the meter
  was not asked about is forgotten, so a reused pid is never measured as the old process;
- error handling: a process that has exited is left out, never raised;
- PG10: psutil only in playout (this module), behind the services' ``ResourceMeter`` Protocol.

No sleeps: the test's own process and child Python processes are measured; a child that must
stay alive blocks on its standard input until the test closes it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator

import pytest

from backend.playout.process_meter import PsutilMeter


@pytest.fixture
def live_child() -> Iterator[int]:
    """A child process that stays alive until the test ends (it waits on its stdin)."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )
    try:
        yield child.pid
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=30)


def test_a_process_is_measured_on_its_second_reading() -> None:
    # T8.7 (D90, D95; M7): warming first, then a processor figure; memory from the start.
    meter = PsutilMeter()
    me = os.getpid()
    [first] = meter.read([me])
    assert (first.pid, first.cpu_percent) == (me, None)
    assert first.memory_mb > 1
    sum(i * i for i in range(200_000))  # some processor time between the readings
    [second] = meter.read([me])
    assert second.pid == me
    assert second.cpu_percent is not None and second.cpu_percent >= 0.0
    assert second.memory_mb > 1


def test_a_process_that_has_exited_is_left_out() -> None:
    # T8.8 (error handling): an engine that exited between the pid list and the reading.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    meter = PsutilMeter()
    assert meter.read([child.pid]) == []
    assert [usage.pid for usage in meter.read([child.pid, os.getpid()])] == [os.getpid()]


def test_the_machine_totals_are_read_and_its_first_cpu_reading_is_warming() -> None:
    # T8.9 (D90, D91; M7): threads and memory for the suggestion; the load is warming first.
    meter = PsutilMeter()
    first = meter.machine()
    assert first.cpu_percent is None
    assert first.threads == os.cpu_count()
    assert first.memory_total_mb > first.memory_available_mb > 0
    second = meter.machine()
    assert second.cpu_percent is not None and 0.0 <= second.cpu_percent <= 100.0
    assert second.threads == first.threads


def test_a_pid_not_asked_for_is_forgotten(live_child: int) -> None:
    # T8.10 (M7: pid reuse): asked for {a, b}, then {b}, then {a} again: a is new again.
    meter = PsutilMeter()
    a, b = live_child, os.getpid()
    meter.read([a, b])
    measured = {usage.pid: usage.cpu_percent for usage in meter.read([a, b])}
    assert measured[a] is not None and measured[b] is not None
    [only_b] = meter.read([b])
    assert only_b.pid == b and only_b.cpu_percent is not None
    [again] = meter.read([a])
    assert (again.pid, again.cpu_percent) == (a, None)
