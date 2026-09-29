"""Start one Liquidsoap session process (spec D6: one process per listener).

Takes primitives only. ``start_session`` does not know about job objects: the caller passes
``assign`` (``KillOnCloseJob.assign`` in production), so this module runs on any OS.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from backend.playout.errors import EngineStartError

__all__ = [
    "SESSION_SCRIPT",
    "EngineConfig",
    "EngineStartError",
    "ScriptCacheError",
    "SessionEndpoint",
    "free_port",
    "launch_env",
    "long_path",
    "session_base_env",
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
