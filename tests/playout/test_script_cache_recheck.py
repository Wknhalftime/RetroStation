"""A failed engine start makes the script cache check itself again (2026-11-08).

On 2026-11-08 Liquidsoap stopped using its own cache for an unchanged ``session.liq``. Every
session then compiled the script, ran past the 5 s readiness deadline (D37) and was killed
before it could save the cache, so every ``/listen`` answered 503 until the cache was warmed
by hand. ``ScriptCache`` warms again only when our files change (D113), so it never noticed.

Required behaviour, through ``warm_first``:

- R1  a start that fails with ``EngineStartError`` warms the unchanged script again;
- R2  the listener gets the start's own error, unchanged;
- R3  the re-warm begins at once, without waiting for another listener, and the failed
      listener's answer does not wait for it;
- R4  a listener who arrives during the re-warm waits for it, and shares it, before its
      engine starts;
- R5  starts that succeed never cause a re-warm;
- R6  a listener who leaves, or a start that fails some other way (a bug), causes none;
- R7  starts failing together share one re-warm;
- R8  a re-warm that fails is reported once to ``on_failure``, the failed listener still
      gets the start's error, and the next listener tries the warm-up again;
- R9  only the start's failure re-warms: a warm-up that fails (a rejected script, an
      overrun build) sets off no second build at once, so D113's handling stands;
- R10 a script edited while the re-warm runs is warmed next, before a start (D113).

The warm-up is the counting stand-in ``Warmer``, so these run on CI. Where a test needs the
re-warm to be running, ``Warmer.gate`` holds it; nothing waits on a sleep to pass.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from backend.playout.errors import EngineStartError
from backend.playout.script_cache import ScriptCache, warm_first
from tests.playout.test_script_cache import Warmer, rejected, scripts

_WAIT_S = 10
_STILL_WAITING_S = 1.0
"""How long R4 gives a listener to (wrongly) start while the re-warm is held. A correct
cache never lets it start, so the bound can only slow a failing run, never flake a pass."""


def not_ready() -> EngineStartError:
    return EngineStartError("harbor on 50123 not ready after 5.0 s; engine killed (exit code 1)")


def could_not_run() -> OSError:
    return OSError("the executable could not be started")


async def warmed(
    tmp_path: Path, warm: Warmer, on_failure: Callable[[BaseException], None] | None = None
) -> ScriptCache:
    """A cache that has done its first warm-up; ``warm.entered`` is cleared afterwards."""
    script = scripts(tmp_path)
    cache = (
        ScriptCache(warm, script) if on_failure is None else ScriptCache(warm, script, on_failure)
    )
    await cache.ensure_warm()
    warm.entered.clear()
    return cache


async def running(warm: Warmer) -> bool:
    """Whether a warm-up has entered ``warm`` (waits up to ``_WAIT_S``)."""
    return await asyncio.to_thread(warm.entered.wait, _WAIT_S)


async def until(condition: Callable[[], bool]) -> None:
    """Return once ``condition`` holds, giving the loop a turn between checks; callers
    bound it with ``wait_for``."""
    while not condition():
        await asyncio.sleep(0)


async def test_a_failed_start_warms_the_unchanged_script_again(tmp_path: Path) -> None:  # R1
    warm = Warmer()
    failures = [not_ready()]
    started: list[str] = []

    async def start(endpoint: str) -> str:
        started.append(endpoint)
        if failures:
            raise failures.pop()
        return endpoint

    begin = warm_first(ScriptCache(warm, scripts(tmp_path)), start)
    with pytest.raises(EngineStartError):
        await begin("s1")
    assert await begin("s2") == "s2"
    assert warm.calls == 2
    assert started == ["s1", "s2"]


async def test_the_listener_gets_the_start_s_own_error(tmp_path: Path) -> None:  # R2
    warm = Warmer()
    failure = not_ready()

    async def start(endpoint: str) -> str:
        raise failure

    cache = ScriptCache(warm, scripts(tmp_path))
    with pytest.raises(EngineStartError) as failed:
        await warm_first(cache, start)("s1")
    assert failed.value is failure
    await cache.ensure_warm()  # let the re-warm finish inside the test


async def test_the_re_warm_begins_at_once_and_the_failure_does_not_wait_for_it(
    tmp_path: Path,
) -> None:  # R3
    warm = Warmer()
    cache = await warmed(tmp_path, warm)

    async def start(endpoint: str) -> str:
        raise not_ready()

    warm.gate = threading.Event()
    try:
        with pytest.raises(EngineStartError):
            # Answered while the re-warm is held: a wait for it would time out here.
            await asyncio.wait_for(warm_first(cache, start)("s1"), _WAIT_S)
        assert await running(warm)  # no other listener has arrived
        assert warm.calls == 2
    finally:
        warm.gate.set()
    await cache.ensure_warm()
    assert warm.calls == 2


async def test_a_listener_arriving_during_the_re_warm_waits_for_it(tmp_path: Path) -> None:  # R4
    warm = Warmer()
    cache = await warmed(tmp_path, warm)
    gate = threading.Event()
    failures = [not_ready()]
    started: list[tuple[str, bool]] = []

    async def start(endpoint: str) -> str:
        started.append((endpoint, gate.is_set()))
        if failures:
            raise failures.pop()
        return endpoint

    begin = warm_first(cache, start)
    warm.gate = gate
    second: asyncio.Future[str] | None = None
    try:
        with pytest.raises(EngineStartError):
            await asyncio.wait_for(begin("s1"), _WAIT_S)
        assert await running(warm)
        second = asyncio.ensure_future(begin("s2"))
        done, _ = await asyncio.wait({second}, timeout=_STILL_WAITING_S)
        assert not done, "s2 started its engine while the re-warm was still running"
        gate.set()
        assert await asyncio.wait_for(second, _WAIT_S) == "s2"
    finally:
        gate.set()
        if second is not None and not second.done():
            second.cancel()
            await asyncio.wait({second})
    assert started == [("s1", False), ("s2", True)]
    assert warm.calls == 2  # s2 shared the re-warm; it did not warm a third time


async def test_starts_that_succeed_never_warm_again(tmp_path: Path) -> None:  # R5
    warm = Warmer()
    cache = ScriptCache(warm, scripts(tmp_path))

    async def start(endpoint: str) -> str:
        return endpoint

    begin = warm_first(cache, start)
    for endpoint in ("s1", "s2", "s3"):
        assert await begin(endpoint) == endpoint
    await cache.ensure_warm()  # would wait for a re-warm, had one begun
    assert warm.calls == 1


@pytest.mark.parametrize(
    "failure",
    [asyncio.CancelledError, lambda: RuntimeError("a bug, not a cold cache")],
    ids=["the listener left", "a bug"],
)
async def test_a_start_that_fails_another_way_does_not_warm_again(
    tmp_path: Path, failure: Callable[[], BaseException]
) -> None:  # R6
    warm = Warmer()
    cache = await warmed(tmp_path, warm)
    raised = failure()

    async def start(endpoint: str) -> str:
        raise raised

    with pytest.raises(type(raised)):
        await warm_first(cache, start)("s1")
    await cache.ensure_warm()  # would wait for a re-warm, had one begun
    assert warm.calls == 1


async def test_starts_failing_together_share_one_re_warm(tmp_path: Path) -> None:  # R7
    warm = Warmer()
    cache = await warmed(tmp_path, warm)
    fail_now = asyncio.Event()
    arrived: list[str] = []
    all_arrived = asyncio.Event()

    async def start(endpoint: str) -> str:
        arrived.append(endpoint)
        if len(arrived) == 3:
            all_arrived.set()
        await fail_now.wait()
        raise not_ready()

    begin = warm_first(cache, start)
    listeners: list[asyncio.Future[str]] = [
        asyncio.ensure_future(begin(f"s{n}")) for n in (1, 2, 3)
    ]
    await asyncio.wait_for(all_arrived.wait(), _WAIT_S)
    warm.gate = threading.Event()  # hold the re-warm while every failure arrives
    try:
        fail_now.set()
        failed = await asyncio.wait_for(asyncio.gather(*listeners, return_exceptions=True), _WAIT_S)
        assert all(isinstance(error, EngineStartError) for error in failed)
        assert await running(warm)
    finally:
        warm.gate.set()
    await cache.ensure_warm()
    assert warm.calls == 2


async def test_a_failed_re_warm_is_reported_and_tried_again(tmp_path: Path) -> None:  # R8
    warm = Warmer()
    reported: list[BaseException] = []
    cache = await warmed(tmp_path, warm, on_failure=reported.append)
    failure = not_ready()
    failures = [failure]

    async def start(endpoint: str) -> str:
        if failures:
            raise failures.pop()
        return endpoint

    begin = warm_first(cache, start)
    warm.fail = could_not_run
    warm.gate = threading.Event()
    try:
        with pytest.raises(EngineStartError) as failed:
            await asyncio.wait_for(begin("s1"), _WAIT_S)
        assert failed.value is failure  # not the re-warm's error
        assert await running(warm)  # the re-warm is held; released, it fails
    finally:
        warm.gate.set()
    await asyncio.wait_for(until(lambda: bool(reported)), _WAIT_S)
    assert len(reported) == 1
    assert isinstance(reported[0], OSError)
    warm.fail = None
    warm.gate = None
    assert await begin("s2") == "s2"
    assert warm.calls == 3


@pytest.mark.parametrize(
    "failure",
    [rejected, lambda: subprocess.TimeoutExpired(["liquidsoap"], 120)],
    ids=["rejected script", "overrun build"],
)
async def test_a_failed_warm_up_does_not_warm_again_at_once(
    tmp_path: Path, failure: Callable[[], BaseException]
) -> None:  # R9
    warm = Warmer()
    reported: list[BaseException] = []
    cache = ScriptCache(warm, scripts(tmp_path), reported.append)
    warm.fail = failure
    started: list[str] = []

    async def start(endpoint: str) -> str:
        started.append(endpoint)
        return endpoint

    with pytest.raises(EngineStartError):
        await warm_first(cache, start)("s1")
    assert started == []
    await asyncio.wait_for(until(lambda: bool(reported)), _WAIT_S)
    # A second build begun at once would fail too and be reported within this window.
    second_report = asyncio.ensure_future(until(lambda: len(reported) > 1))
    done, _ = await asyncio.wait({second_report}, timeout=_STILL_WAITING_S)
    second_report.cancel()
    await asyncio.wait({second_report})
    assert not done, "a failed warm-up set off another build at once"
    assert warm.calls == 1


async def test_a_script_edited_during_the_re_warm_is_warmed_next(tmp_path: Path) -> None:  # R10
    warm = Warmer()
    cache = await warmed(tmp_path, warm)
    failures = [not_ready()]

    async def start(endpoint: str) -> str:
        if failures:
            raise failures.pop()
        return endpoint

    begin = warm_first(cache, start)
    warm.gate = threading.Event()
    try:
        with pytest.raises(EngineStartError):
            await asyncio.wait_for(begin("s1"), _WAIT_S)
        assert await running(warm)
        with (tmp_path / "session.liq").open("a", encoding="utf-8") as script:
            script.write("y = 2\n")  # D113: the edited script is the one to warm
    finally:
        warm.gate.set()
    assert await asyncio.wait_for(begin("s2"), _WAIT_S) == "s2"
    assert warm.calls == 3  # the first warm-up, the re-warm, the edited script
    await cache.ensure_warm()
    assert warm.calls == 3
