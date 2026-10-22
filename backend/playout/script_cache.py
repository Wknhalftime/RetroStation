"""Keep Liquidsoap's script cache warm for the session script on disk now (D113).

The app warms the cache at startup (D34). An edit to ``session.liq``, or to a file it
includes, while the app runs left it cold: a cold compile takes about 5.3 s, over D37's 5 s
readiness deadline, so every session was killed before it could write the cache, and every
``/listen`` failed until a restart. ``ScriptCache`` fingerprints the script before each start
and warms the cache again when the fingerprint changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from backend.playout.errors import EngineStartError
from backend.playout.liquidsoap_process import ScriptCacheError

__all__ = ["ScriptCache", "script_fingerprint", "warm_first"]

_INCLUDE = re.compile(rb'^[ \t]*%include[ \t]+"([^"]+)"', re.MULTILINE)
# What warm_script_cache raises: the script was rejected, the build overran, or it never ran.
_WARM_FAILURES = (ScriptCacheError, subprocess.TimeoutExpired, OSError)


def script_fingerprint(script: Path) -> str:
    """A hash of ``script`` and every file it ``%include``s, by resolved path and content.

    The path counts because Liquidsoap's cache key includes it. Includes are followed from
    the including file's folder, each file once. ``%include <...>`` (Liquidsoap's own
    library) is not followed: it changes only with the executable. Raises ``OSError`` if a
    file cannot be read.
    """
    digest = hashlib.sha256()
    pending = [script.resolve()]
    seen: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        text = current.read_bytes()
        digest.update(str(current).encode() + b"\0" + hashlib.sha256(text).digest())
        names = _INCLUDE.findall(text)
        pending.extend((current.parent / name.decode()).resolve() for name in reversed(names))
    return digest.hexdigest()


def _ignore(error: BaseException) -> None:
    return None


@dataclass(frozen=True)
class _Build:
    fingerprint: str
    task: asyncio.Task[None]


def _rejected(build: _Build) -> bool:
    """Liquidsoap refused this build's script, which fails the same way until it changes."""
    task = build.task
    return task.done() and not task.cancelled() and isinstance(task.exception(), ScriptCacheError)


class ScriptCache:
    """The script cache for one session script, warmed again whenever the script changes.

    ``warm`` builds the cache for the script on disk (``warm_script_cache`` in production).
    One build runs at a time, and every start waiting on it shares it; a start that is
    cancelled leaves the build running. A script Liquidsoap rejected fails every start at
    once until it changes; a build that overran or could not run is tried again by the next
    start. ``on_failure`` is told about each failed build once.
    """

    def __init__(
        self,
        warm: Callable[[], None],
        script: Path,
        on_failure: Callable[[BaseException], None] = _ignore,
    ) -> None:
        self._warm = warm
        self._script = script
        self._on_failure = on_failure
        self._warm_for: str | None = None
        self._build: _Build | None = None

    async def ensure_warm(self) -> None:
        """Return once the cache holds the script on disk now.

        Raises ``EngineStartError`` if the script cannot be read or its build failed.
        """
        while True:
            fingerprint = await self._fingerprint()
            if fingerprint == self._warm_for:
                return
            build = self._current(fingerprint)
            await asyncio.wait({build.task})  # never cancels the shared build
            if build.fingerprint == fingerprint and (failure := build.task.exception()):
                raise self._start_failed(failure)
            # Ours succeeded, or an older script's build finished: look at the script again.

    async def _fingerprint(self) -> str:
        try:
            return await asyncio.to_thread(script_fingerprint, self._script)
        except OSError as unreadable:
            raise EngineStartError(
                f"the session script could not be read: {unreadable}"
            ) from unreadable

    def _current(self, fingerprint: str) -> _Build:
        """The build in progress, this script's rejected build, or a new build of it."""
        build = self._build
        if build is not None and not build.task.done():
            return build
        if build is not None and build.fingerprint == fingerprint and _rejected(build):
            return build
        task = asyncio.ensure_future(self._compile(fingerprint))
        task.add_done_callback(self._settled)
        self._build = _Build(fingerprint, task)
        return self._build

    async def _compile(self, fingerprint: str) -> None:
        await asyncio.to_thread(self._warm)
        self._warm_for = fingerprint

    def _settled(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (failure := task.exception()) is not None:
            self._on_failure(failure)

    @staticmethod
    def _start_failed(failure: BaseException) -> BaseException:
        if not isinstance(failure, _WARM_FAILURES):
            return failure  # not a build failure: a bug, raised as it is
        error = EngineStartError(f"Liquidsoap's script cache could not be built: {failure}")
        error.__cause__ = failure
        return error


def warm_first[E, R](
    cache: ScriptCache, start: Callable[[E], Awaitable[R]]
) -> Callable[[E], Awaitable[R]]:
    """``start``, run once ``cache`` holds the script on disk now."""

    async def started(endpoint: E) -> R:
        await cache.ensure_warm()
        return await start(endpoint)

    return started
