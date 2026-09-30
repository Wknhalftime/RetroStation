"""Start one Liquidsoap session process (spec D6: one process per listener).

Takes primitives only. ``start_session`` does not know about job objects: the caller passes
``assign`` (``KillOnCloseJob.assign`` in production), so this module runs on any OS.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from backend.playout.errors import EngineStartError
from backend.playout.harbor import Upstream, open_upstream

__all__ = [
    "SESSION_LOG_KEEP",
    "SESSION_SCRIPT",
    "EngineConfig",
    "EngineStartError",
    "RunningEngine",
    "ScriptCacheError",
    "SessionEndpoint",
    "free_port",
    "launch_env",
    "long_path",
    "prune_session_logs",
    "session_base_env",
    "start_ready_engine",
    "start_session",
    "warm_script_cache",
]

SESSION_SCRIPT = Path(__file__).with_name("session.liq")
_BACKSLASH_DIGIT = re.compile(r"\\\d")
if sys.platform == "win32":  # an if-statement, so mypy --platform linux skips the name
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW
else:
    _NO_WINDOW = 0
_CACHE_VARS = ("LIQ_CACHE_DIR", "LIQ_CACHE_USER_DIR", "LIQ_CACHE_SYSTEM_DIR")
_CACHE_BUILD_TIMEOUT_S = 120
_KILL_WAIT_S = 10
SESSION_LOG_KEEP = 50  # D35: per-session engine logs, kept at app start
_LONG_PREFIX = "\\\\?\\"
_UNC_PREFIX = _LONG_PREFIX + "UNC\\"
# What the engine needs from the API's environment, and nothing else: PATH for DLL lookup,
# SYSTEMROOT/WINDIR for Winsock, TEMP/TMP for temporary files. Secrets never reach it.
_ENGINE_ENV_VARS = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")


def long_path(path: Path) -> str:
    """The path with the ``\\\\?\\`` prefix (Liquidsoap needs it past 260 chars).

    A relative path is resolved; an absolute one is only normalised, because resolving a
    network path touches the network. A share (``\\\\host\\share``) takes the
    ``\\\\?\\UNC\\`` form; an already prefixed path is returned unchanged.
    """
    text = str(path) if path.is_absolute() else str(path.resolve())
    if text.startswith(_LONG_PREFIX):
        return text
    text = os.path.normpath(text)
    if text.startswith("\\\\"):
        return _UNC_PREFIX + text[2:]
    return _LONG_PREFIX + text


def session_base_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The allow-listed part of ``environ`` a session engine is started with."""
    return {name: environ[name] for name in _ENGINE_ENV_VARS if name in environ}


def free_port() -> int:
    """A loopback TCP port that was free a moment ago (the OS picks it)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def prune_session_logs(log_dir: Path, keep: int = SESSION_LOG_KEEP) -> None:
    """Delete every ``*.log`` in ``log_dir`` past the newest ``keep`` (D35).

    A missing folder is a no-op. Files are ordered by ``(mtime, name)``, newest first;
    only ``*.log`` files are considered, so unrelated files in the folder are untouched.
    """
    if not log_dir.is_dir():
        return
    logs = sorted(
        log_dir.glob("*.log"), key=lambda log: (log.stat().st_mtime, log.name), reverse=True
    )
    for stale in logs[keep:]:
        stale.unlink()


@dataclass(frozen=True)
class EngineConfig:
    """How every session process is run: one per app, built at the composition root."""

    exe: Path
    script: Path
    cache_dir: Path
    filler: Path
    intro_sfx: Path | None
    intro_fade_at_s: float = 1.0
    no_client_exit_s: float = 15.0
    ready_timeout_s: float = 5.0  # D37

    def __post_init__(self) -> None:
        if _BACKSLASH_DIGIT.search(str(self.exe)):
            raise ValueError(
                "EngineConfig.exe must not contain a backslash followed by a digit "
                f"(Liquidsoap 2.4.5 crashes at startup): {self.exe}"
            )
        if self.intro_fade_at_s < 0:
            raise ValueError(
                f"EngineConfig.intro_fade_at_s must be >= 0, got {self.intro_fade_at_s}"
            )
        if self.no_client_exit_s <= 0:
            raise ValueError(
                f"EngineConfig.no_client_exit_s must be > 0, got {self.no_client_exit_s}"
            )
        if self.ready_timeout_s <= 0:
            raise ValueError(
                f"EngineConfig.ready_timeout_s must be > 0, got {self.ready_timeout_s}"
            )


@dataclass(frozen=True)
class SessionEndpoint:
    """Where one session listens and whom it calls back."""

    session_id: str
    harbor_port: int
    backend_url: str
    session_token: str
    log_path: Path

    def __post_init__(self) -> None:
        if not 1024 <= self.harbor_port <= 65535:
            raise ValueError(
                f"SessionEndpoint.harbor_port must be 1024..65535, got {self.harbor_port}"
            )


def _cache_env(cache_dir: Path) -> dict[str, str]:
    cache = str(cache_dir.resolve())
    return {name: cache for name in _CACHE_VARS}


def launch_env(
    base: Mapping[str, str], endpoint: SessionEndpoint, engine: EngineConfig
) -> dict[str, str]:
    """``base`` plus the variables ``session.liq`` reads."""
    return {
        **base,
        "SESSION_ID": endpoint.session_id,
        "HARBOR_PORT": str(endpoint.harbor_port),
        "BACKEND_URL": endpoint.backend_url,
        "SESSION_TOKEN": endpoint.session_token,
        "FILLER": long_path(engine.filler),
        "INTRO_SFX": long_path(engine.intro_sfx) if engine.intro_sfx else "",
        "INTRO_FADE_AT": str(engine.intro_fade_at_s),
        "NO_CLIENT_EXIT_S": str(engine.no_client_exit_s),
        **_cache_env(engine.cache_dir),
    }


class ScriptCacheError(subprocess.CalledProcessError):
    """Liquidsoap rejected the script; the message carries what it printed.

    Liquidsoap 2.4.5 prints compile errors on stdout, so both streams are shown.
    """

    def __str__(self) -> str:
        printed = [
            f"{name}:\n{stream.decode(errors='replace').strip()}"
            for name, stream in (("stderr", self.stderr), ("stdout", self.output))
            if stream and stream.strip()
        ]
        return "\n".join([super().__str__(), *printed])


def warm_script_cache(base_env: Mapping[str, str], engine: EngineConfig) -> None:
    """Type-check ``engine.script`` into ``engine.cache_dir`` without running it.

    A cold cache costs about 5 s at every session start; warm, it loads in under 0.1 s.
    Raises ``ScriptCacheError`` (a ``subprocess.CalledProcessError``) with Liquidsoap's
    output if the script does not compile, and ``subprocess.TimeoutExpired`` if the build
    takes longer than ``_CACHE_BUILD_TIMEOUT_S``.
    """
    command = [str(engine.exe), "--cache-only", str(engine.script)]
    build = subprocess.run(
        command,
        env={**base_env, **_cache_env(engine.cache_dir)},
        capture_output=True,
        timeout=_CACHE_BUILD_TIMEOUT_S,
        creationflags=_NO_WINDOW,
    )
    if build.returncode != 0:
        raise ScriptCacheError(build.returncode, command, build.stdout, build.stderr)


def start_session(
    assign: Callable[[int], None],
    base_env: Mapping[str, str],
    endpoint: SessionEndpoint,
    engine: EngineConfig,
) -> subprocess.Popen[bytes]:
    """Start the process and hand its pid to ``assign``; kill it if ``assign`` fails.

    ``assign``'s ``OSError`` is re-raised. If the killed process does not exit within
    ``_KILL_WAIT_S``, that is noted on the ``OSError`` rather than replacing it.
    """
    with endpoint.log_path.open("ab") as log:
        process = subprocess.Popen(
            [str(engine.exe), str(engine.script)],
            env=launch_env(base_env, endpoint, engine),
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
    try:
        assign(process.pid)
    except OSError as refused:
        process.kill()
        try:
            process.wait(timeout=_KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            refused.add_note(
                f"engine process {process.pid} was killed but had not exited after {_KILL_WAIT_S} s"
            )
        raise
    return process


@dataclass(frozen=True)
class RunningEngine:
    """A session engine whose harbor answered 200: its pid, port, stop and audio.

    ``stop`` kills the process if it is still alive and returns at once; it never waits,
    because it is called from the event loop. It does not close ``upstream``: callers must
    call ``upstream.close()`` as well.
    """

    pid: int
    port: int
    stop: Callable[[], None]
    upstream: Upstream


def _stopper(process: subprocess.Popen[bytes]) -> Callable[[], None]:
    def stop() -> None:
        if process.poll() is None:
            process.kill()

    return stop


def _reap(process: subprocess.Popen[bytes]) -> str:
    """Kill ``process`` if alive and wait for its exit code (blocking: use a worker thread).

    The text says whether we killed it (``engine killed (exit code N)``) or it had exited by
    itself (``engine exit code N``).
    """
    killed = process.poll() is None
    if killed:
        process.kill()
    try:
        code = process.wait(timeout=_KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        return f"engine process {process.pid} was killed but had not exited after {_KILL_WAIT_S} s"
    return f"engine killed (exit code {code})" if killed else f"engine exit code {code}"


def _abandon(process: subprocess.Popen[bytes]) -> None:
    """Kill ``process`` now and reap it in a background thread, without waiting here."""
    if process.poll() is None:
        process.kill()
    threading.Thread(target=_reap, args=(process,), name="engine-reap", daemon=True).start()


def _abandon_started(starting: asyncio.Future[subprocess.Popen[bytes]]) -> None:
    """Done-callback: abandon the process a start nobody is waiting for produced."""
    if not starting.cancelled() and starting.exception() is None:
        _abandon(starting.result())


async def _spawn(
    assign: Callable[[int], None],
    base_env: Mapping[str, str],
    endpoint: SessionEndpoint,
    engine: EngineConfig,
) -> subprocess.Popen[bytes]:
    """``start_session`` in a worker thread; if we are cancelled, its process is abandoned.

    The thread cannot be stopped, so it is shielded and its result killed when it arrives.
    """
    starting = asyncio.ensure_future(
        asyncio.to_thread(start_session, assign, base_env, endpoint, engine)
    )
    claimed = False
    try:
        process = await asyncio.shield(starting)
        claimed = True
    finally:
        if not claimed:
            starting.add_done_callback(_abandon_started)
    return process


def _attempt_failed(message: str, cause: BaseException) -> EngineStartError:
    """An attempt failure whose ``__cause__`` is what went wrong."""
    failed = EngineStartError(message)
    failed.__cause__ = cause
    return failed


def _with_notes(error: BaseException) -> str:
    """``error``'s text followed by any notes added to it."""
    return "; ".join([str(error), *getattr(error, "__notes__", ())])


async def _attempt(
    assign: Callable[[int], None],
    base_env: Mapping[str, str],
    endpoint: SessionEndpoint,
    engine: EngineConfig,
) -> RunningEngine | EngineStartError:
    """One start: the engine once its harbor answers 200, or why it did not.

    On every other way out (not ready, cancelled, an unexpected error) the process is killed.
    """
    try:
        process = await _spawn(assign, base_env, endpoint, engine)
    except OSError as refused:
        return _attempt_failed(f"engine did not start: {_with_notes(refused)}", refused)
    port = endpoint.harbor_port
    ready = False
    try:
        upstream = await open_upstream(
            port, engine.ready_timeout_s, alive=lambda: process.poll() is None
        )
        ready = True
    except EngineStartError as not_ready:
        reaped = await asyncio.to_thread(_reap, process)
        return _attempt_failed(f"{not_ready}; {reaped}", not_ready)
    finally:
        if not ready and process.returncode is None:
            _abandon(process)
    return RunningEngine(pid=process.pid, port=port, stop=_stopper(process), upstream=upstream)


def _another_port(taken: int) -> int:
    """A free loopback port other than ``taken``."""
    port = free_port()
    while port == taken:
        port = free_port()
    return port


async def start_ready_engine(
    assign: Callable[[int], None],
    base_env: Mapping[str, str],
    endpoint: SessionEndpoint,
    engine: EngineConfig,
) -> RunningEngine:
    """Start a session engine and wait until its harbor serves audio (D37).

    An attempt that fails to start, exits, or is not ready within ``engine.ready_timeout_s``
    is killed, reaped in a worker thread, and retried once on another port. Apart from
    cancellation, only ``EngineStartError`` escapes: it names both failures, or the first
    failure and why no port was found for the retry.
    """
    first = await _attempt(assign, base_env, endpoint, engine)
    if isinstance(first, RunningEngine):
        return first
    try:
        retry_port = _another_port(endpoint.harbor_port)
    except OSError as no_port:
        raise EngineStartError(
            f"session {endpoint.session_id}: {first}; no port to retry on: {no_port}"
        ) from no_port
    retry = replace(endpoint, harbor_port=retry_port)
    second = await _attempt(assign, base_env, retry, engine)
    if isinstance(second, RunningEngine):
        return second
    raise EngineStartError(
        f"session {endpoint.session_id}: {first}; retry on port {retry.harbor_port}: {second}"
    ) from second
