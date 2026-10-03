"""A start that nobody waits for any more leaves no engine behind (spec D6: "gone within ~1 s
of disconnect. Nothing runs with no listeners"; D1 review: give start_ready_engine's
kill-on-cancel a locked home). Extends the locked tests/playout/test_engine_start.py and uses
its Python stand-in for Liquidsoap, so these run on CI.

Both tests order their steps by events the stand-in or the start itself produces (a bound
harbor, an assigned pid), never by sleeping."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import psutil

from backend.playout.liquidsoap_process import start_ready_engine
from tests.playout.test_engine_start import BIND, NEVER_READY, fake_engine

GONE_WITHIN_S = 1.0  # D6
HARBOR_THAT_NEVER_ANSWERS = BIND + "time.sleep(60)\n"  # listening, but no reply ever comes


def gone_within(pid: int, timeout_s: float) -> bool:
    """Whether process ``pid`` has exited within ``timeout_s`` (blocking; run in a thread)."""
    try:
        psutil.Process(pid).wait(timeout=timeout_s)
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        return False
    return True


async def until(condition: Callable[[], bool], timeout_s: float) -> bool:
    """Waits for an outcome up to a deadline (not an ordering step)."""
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def test_cancelling_a_start_that_waits_for_the_harbor_kills_the_engine(
    tmp_path: Path,
) -> None:
    engine, endpoint = fake_engine(tmp_path, HARBOR_THAT_NEVER_ANSWERS, ready_timeout_s=10.0)
    pids: list[int] = []
    starting = asyncio.create_task(start_ready_engine(pids.append, os.environ, endpoint, engine))
    try:
        served = tmp_path / "engine.py.served"
        assert await until(served.exists, 10.0), "harness: the stand-in never bound its harbor"
        assert not starting.done(), "harness: the start ended before it was cancelled"
        starting.cancel()
        await asyncio.wait({starting})
        assert starting.cancelled()
        for pid in pids:
            assert await asyncio.to_thread(gone_within, pid, GONE_WITHIN_S), (
                f"engine {pid} still running {GONE_WITHIN_S} s after the start was cancelled"
            )
    finally:
        for pid in pids:
            if psutil.pid_exists(pid):
                psutil.Process(pid).kill()


async def test_cancelling_a_start_that_is_still_spawning_kills_the_engine(
    tmp_path: Path,
) -> None:
    engine, endpoint = fake_engine(tmp_path, NEVER_READY, ready_timeout_s=10.0)
    loop = asyncio.get_running_loop()
    pids: list[int] = []
    spawned = asyncio.Event()
    release = threading.Event()

    def slow_assign(pid: int) -> None:
        """The job assignment is still running (in D1's worker thread) when the start is
        cancelled."""
        pids.append(pid)
        loop.call_soon_threadsafe(spawned.set)
        release.wait(timeout=10.0)

    starting = asyncio.create_task(start_ready_engine(slow_assign, os.environ, endpoint, engine))
    try:
        await asyncio.wait_for(spawned.wait(), timeout=10.0)
        starting.cancel()
        await asyncio.wait({starting})
        assert starting.cancelled()
        [pid] = pids
        release.set()  # the spawn finishes after nobody waits for it any more
        assert await asyncio.to_thread(gone_within, pid, GONE_WITHIN_S), (
            f"engine {pid} still running {GONE_WITHIN_S} s after its spawn finished"
        )
    finally:
        release.set()
        for pid in pids:
            if psutil.pid_exists(pid):
                psutil.Process(pid).kill()
