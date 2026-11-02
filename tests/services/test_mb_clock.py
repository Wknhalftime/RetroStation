"""The machine-wide MusicBrainz clock spaces calls across threads and processes."""

from __future__ import annotations

import itertools
import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from backend.services.mb_clock import wait_turn


def test_second_call_waits_the_cadence(tmp_path: Path) -> None:
    clock = tmp_path / "clock"
    wait_turn(clock, cadence_s=0.3)
    t0 = time.monotonic()
    wait_turn(clock, cadence_s=0.3)
    assert time.monotonic() - t0 >= 0.29


def test_missing_clock_file_means_no_previous_call(tmp_path: Path) -> None:
    clock = tmp_path / "clock"
    t0 = time.monotonic()
    wait_turn(clock, cadence_s=5.0)  # must not sleep: nothing to space from
    assert time.monotonic() - t0 < 1.0
    assert clock.exists()


def test_two_threads_are_spaced(tmp_path: Path) -> None:
    clock = tmp_path / "clock"
    stamps: list[float] = []

    def worker() -> None:
        for _ in range(2):
            wait_turn(clock, cadence_s=0.3)
            stamps.append(time.time())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(stamps) == 4
    assert all(b - a >= 0.29 for a, b in itertools.pairwise(sorted(stamps)))


def _stamp_calls(clock: Path, stamps_file: Path, calls: int, cadence_s: float) -> None:
    """Child-process target (module level so Windows spawn can pickle it)."""
    for _ in range(calls):
        wait_turn(clock, cadence_s=cadence_s)
        with stamps_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.time()!r}\n")


@pytest.mark.slow
def test_two_processes_are_spaced(tmp_path: Path) -> None:
    clock = tmp_path / "clock"
    stamps_file = tmp_path / "stamps.txt"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_stamp_calls, args=(clock, stamps_file, 3, 0.5)) for _ in range(2)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0

    stamps = sorted(float(line) for line in stamps_file.read_text().splitlines())
    assert len(stamps) == 6
    assert all(b - a >= 0.49 for a, b in itertools.pairwise(stamps))
