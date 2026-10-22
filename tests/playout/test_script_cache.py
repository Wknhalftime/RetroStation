"""Keeping Liquidsoap's script cache warm for the script on disk now (D113).

The warm-up is injected: a counting stand-in for ``warm_script_cache``, so these run on CI.
The real compile is ``test_script_cache_live.py``.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from backend.playout.errors import EngineStartError
from backend.playout.liquidsoap_process import ScriptCacheError
from backend.playout.script_cache import ScriptCache, script_fingerprint, warm_first


def scripts(tmp_path: Path) -> Path:
    """``session.liq`` including ``agc.liq``, which includes ``lib/inner.liq``."""
    (tmp_path / "lib").mkdir(parents=True)
    (tmp_path / "lib" / "inner.liq").write_text("inner = 1\n", encoding="utf-8")
    (tmp_path / "agc.liq").write_text('%include "lib/inner.liq"\nagc = 1\n', encoding="utf-8")
    session = tmp_path / "session.liq"
    session.write_text('x = 1\n%include "agc.liq"\n', encoding="utf-8")
    return session


class Warmer:
    """Counts warm-ups; ``fail`` raises instead; ``gate`` (if set) holds each one."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail: Callable[[], BaseException] | None = None
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def __call__(self) -> None:
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10), "the test never released the warm-up"
        if self.fail is not None:
            raise self.fail()


def rejected() -> ScriptCacheError:
    return ScriptCacheError(1, ["liquidsoap"], b"Error 5: this value has type float", b"")


def test_the_fingerprint_is_stable_while_nothing_changes(tmp_path: Path) -> None:
    session = scripts(tmp_path)
    assert script_fingerprint(session) == script_fingerprint(session)


@pytest.mark.parametrize("edited", ["session.liq", "agc.liq", "lib/inner.liq"])
def test_the_fingerprint_changes_when_the_script_or_an_include_changes(
    tmp_path: Path, edited: str
) -> None:
    session = scripts(tmp_path)
    before = script_fingerprint(session)
    with (tmp_path / edited).open("a", encoding="utf-8") as script:
        script.write("y = 2\n")
    assert script_fingerprint(session) != before


def test_the_fingerprint_ignores_files_that_are_not_included(tmp_path: Path) -> None:
    session = scripts(tmp_path)
    before = script_fingerprint(session)
    (tmp_path / "cue_analysis.liq").write_text("z = 3\n", encoding="utf-8")
    assert script_fingerprint(session) == before


def test_the_same_script_at_another_path_has_another_fingerprint(tmp_path: Path) -> None:
    """Liquidsoap's cache key includes the script's path, so a moved script is cold."""
    first = scripts(tmp_path / "a")
    second = scripts(tmp_path / "b")
    assert script_fingerprint(first) != script_fingerprint(second)


def test_an_include_cycle_is_read_once(tmp_path: Path) -> None:
    session = tmp_path / "session.liq"
    session.write_text('%include "other.liq"\n', encoding="utf-8")
    (tmp_path / "other.liq").write_text('%include "session.liq"\n', encoding="utf-8")
    assert script_fingerprint(session)


def test_a_missing_include_is_an_os_error(tmp_path: Path) -> None:
    session = tmp_path / "session.liq"
    session.write_text('%include "gone.liq"\n', encoding="utf-8")
    with pytest.raises(OSError):
        script_fingerprint(session)


async def test_an_unchanged_script_is_warmed_once(tmp_path: Path) -> None:
    warm = Warmer()
    cache = ScriptCache(warm, scripts(tmp_path))
    await cache.ensure_warm()
    await cache.ensure_warm()
    assert warm.calls == 1


@pytest.mark.parametrize("edited", ["session.liq", "lib/inner.liq"])
async def test_an_edited_script_is_warmed_again(tmp_path: Path, edited: str) -> None:
    warm = Warmer()
    cache = ScriptCache(warm, scripts(tmp_path))
    await cache.ensure_warm()
    with (tmp_path / edited).open("a", encoding="utf-8") as script:
        script.write("y = 2\n")
    await cache.ensure_warm()
    await cache.ensure_warm()
    assert warm.calls == 2


async def test_listeners_arriving_together_share_one_warm_up(tmp_path: Path) -> None:
    warm = Warmer()
    warm.gate = threading.Event()
    cache = ScriptCache(warm, scripts(tmp_path))
    waiting = [asyncio.create_task(cache.ensure_warm()) for _ in range(5)]
    assert await asyncio.to_thread(warm.entered.wait, 10)
    warm.gate.set()
    await asyncio.gather(*waiting)
    assert warm.calls == 1


async def test_a_listener_that_leaves_does_not_cancel_the_warm_up(tmp_path: Path) -> None:
    warm = Warmer()
    warm.gate = threading.Event()
    cache = ScriptCache(warm, scripts(tmp_path))
    leaving = asyncio.create_task(cache.ensure_warm())
    assert await asyncio.to_thread(warm.entered.wait, 10)
    leaving.cancel()
    staying = asyncio.create_task(cache.ensure_warm())
    warm.gate.set()
    await staying
    assert warm.calls == 1
    with pytest.raises(asyncio.CancelledError):
        await leaving


async def test_a_script_liquidsoap_rejects_fails_the_start_with_its_output(
    tmp_path: Path,
) -> None:
    warm = Warmer()
    warm.fail = rejected
    cache = ScriptCache(warm, scripts(tmp_path))
    with pytest.raises(EngineStartError, match="this value has type float") as failed:
        await cache.ensure_warm()
    assert isinstance(failed.value.__cause__, ScriptCacheError)


async def test_a_rejected_script_is_not_compiled_again_until_it_changes(tmp_path: Path) -> None:
    warm = Warmer()
    warm.fail = rejected
    session = scripts(tmp_path)
    cache = ScriptCache(warm, session)
    for _ in range(3):
        with pytest.raises(EngineStartError, match="this value has type float"):
            await cache.ensure_warm()
    assert warm.calls == 1
    warm.fail = None
    with session.open("a", encoding="utf-8") as script:
        script.write("# fixed\n")
    await cache.ensure_warm()
    assert warm.calls == 2


@pytest.mark.parametrize(
    "failure",
    [
        lambda: subprocess.TimeoutExpired(["liquidsoap"], 120),
        lambda: OSError("the executable could not be started"),
    ],
)
async def test_a_warm_up_that_could_not_run_is_tried_again_by_the_next_listener(
    tmp_path: Path, failure: Callable[[], BaseException]
) -> None:
    warm = Warmer()
    warm.fail = failure
    cache = ScriptCache(warm, scripts(tmp_path))
    with pytest.raises(EngineStartError):
        await cache.ensure_warm()
    warm.fail = None
    await cache.ensure_warm()
    assert warm.calls == 2


async def test_an_unreadable_script_fails_the_start(tmp_path: Path) -> None:
    warm = Warmer()
    session = scripts(tmp_path)
    cache = ScriptCache(warm, session)
    (tmp_path / "agc.liq").unlink()
    with pytest.raises(EngineStartError, match="agc.liq"):
        await cache.ensure_warm()
    assert warm.calls == 0


async def test_warm_first_warms_before_it_starts(tmp_path: Path) -> None:
    warm = Warmer()
    seen: list[tuple[int, str]] = []

    async def start(endpoint: str) -> str:
        seen.append((warm.calls, endpoint))
        return f"engine for {endpoint}"

    started = await warm_first(ScriptCache(warm, scripts(tmp_path)), start)("s1")
    assert started == "engine for s1"
    assert seen == [(1, "s1")]


async def test_warm_first_does_not_start_when_the_warm_up_fails(tmp_path: Path) -> None:
    warm = Warmer()
    warm.fail = rejected
    started: list[str] = []

    async def start(endpoint: str) -> str:
        started.append(endpoint)
        return endpoint

    with pytest.raises(EngineStartError):
        await warm_first(ScriptCache(warm, scripts(tmp_path)), start)("s1")
    assert started == []
