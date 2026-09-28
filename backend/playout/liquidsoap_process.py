"""Start one Liquidsoap session process (spec D6: one process per listener).

Takes primitives only. ``start_session`` does not know about job objects: the caller passes
``assign`` (``KillOnCloseJob.assign`` in production), so this module runs on any OS.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

SESSION_SCRIPT = Path(__file__).with_name("session.liq")
_BACKSLASH_DIGIT = re.compile(r"\\\d")
if sys.platform == "win32":  # an if-statement, so mypy --platform linux skips the name
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW
else:
    _NO_WINDOW = 0
_CACHE_VARS = ("LIQ_CACHE_DIR", "LIQ_CACHE_USER_DIR", "LIQ_CACHE_SYSTEM_DIR")
_CACHE_BUILD_TIMEOUT_S = 120


def long_path(path: Path) -> str:
    """Absolute path with the ``\\\\?\\`` prefix (Liquidsoap needs it past 260 chars)."""
    return "\\\\?\\" + str(path.resolve())


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


def warm_script_cache(base_env: Mapping[str, str], engine: EngineConfig) -> None:
    """Type-check ``engine.script`` into ``engine.cache_dir`` without running it.

    A cold cache costs about 5 s at every session start; warm, it loads in under 0.1 s.
    Raises ``subprocess.CalledProcessError`` if the script does not compile.
    """
    subprocess.run(
        [str(engine.exe), "--cache-only", str(engine.script)],
        env={**base_env, **_cache_env(engine.cache_dir)},
        capture_output=True,
        check=True,
        timeout=_CACHE_BUILD_TIMEOUT_S,
        creationflags=_NO_WINDOW,
    )


def start_session(
    assign: Callable[[int], None],
    base_env: Mapping[str, str],
    endpoint: SessionEndpoint,
    engine: EngineConfig,
) -> subprocess.Popen[bytes]:
    """Start the process and hand its pid to ``assign``; kill it if ``assign`` fails."""
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
    except OSError:
        process.kill()
        process.wait(timeout=10)
        raise
    return process
