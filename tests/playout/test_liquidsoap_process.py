"""Session launcher: environment contract and process lifecycle (spec: Engine, D6)."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from backend.playout.liquidsoap_process import (
    SESSION_SCRIPT,
    EngineConfig,
    SessionEndpoint,
    launch_env,
    start_session,
)

# The venv's python.exe on Windows is a launcher with the real interpreter as its child;
# killing the launcher would leave that child behind, so run the real interpreter.
PYTHON = Path(getattr(sys, "_base_executable", sys.executable))

ENGINE = EngineConfig(
    exe=Path("liquidsoap.exe"),
    script=SESSION_SCRIPT,
    cache_dir=Path("cache"),
    filler=Path("filler.flac"),
    intro_sfx=Path("static_intro.flac"),
)
ENDPOINT = SessionEndpoint(
    session_id="s1",
    harbor_port=18040,
    backend_url="http://127.0.0.1:8000/internal/stream/sessions/s1",
    session_token="tok",
    log_path=Path("s1.log"),
)
LONG_PATH_MARKER = "\\\\?\\"


def test_session_script_ships_with_the_package() -> None:
    assert SESSION_SCRIPT.name == "session.liq"
    assert SESSION_SCRIPT.parent.name == "playout"
    assert SESSION_SCRIPT.is_file()


def test_launch_env_passes_session_values_and_keeps_base() -> None:
    env = launch_env({"PATH": "/usr/bin", "SESSION_ID": "stale"}, ENDPOINT, ENGINE)
    assert env["PATH"] == "/usr/bin"
    assert env["SESSION_ID"] == "s1"
    assert env["HARBOR_PORT"] == "18040"
    assert env["BACKEND_URL"] == "http://127.0.0.1:8000/internal/stream/sessions/s1"
    assert env["SESSION_TOKEN"] == "tok"
    assert float(env["INTRO_FADE_AT"]) == 1.0
    assert float(env["NO_CLIENT_EXIT_S"]) == 15.0


def test_launch_env_prefixes_long_path_marker() -> None:
    env = launch_env({}, ENDPOINT, ENGINE)
    assert env["FILLER"] == LONG_PATH_MARKER + str(Path("filler.flac").resolve())
    assert env["INTRO_SFX"] == LONG_PATH_MARKER + str(Path("static_intro.flac").resolve())


def test_launch_env_without_intro_is_empty_string() -> None:
    assert launch_env({}, ENDPOINT, replace(ENGINE, intro_sfx=None))["INTRO_SFX"] == ""


def test_launch_env_points_liquidsoap_cache_at_cache_dir() -> None:
    env = launch_env({}, ENDPOINT, ENGINE)
    for name in ("LIQ_CACHE_DIR", "LIQ_CACHE_USER_DIR", "LIQ_CACHE_SYSTEM_DIR"):
        assert Path(env[name]).resolve() == Path("cache").resolve()


def test_endpoint_rejects_privileged_port() -> None:
    with pytest.raises(ValueError, match="SessionEndpoint.harbor_port"):
        replace(ENDPOINT, harbor_port=80)


def test_engine_rejects_backslash_digit_in_exe_path() -> None:
    with pytest.raises(ValueError, match="EngineConfig.exe"):
        replace(ENGINE, exe=Path(r"D:\tools\2024\liquidsoap.exe"))


def test_engine_rejects_negative_intro_fade_at() -> None:
    with pytest.raises(ValueError, match="EngineConfig.intro_fade_at_s"):
        replace(ENGINE, intro_fade_at_s=-0.5)


def test_engine_rejects_non_positive_no_client_exit() -> None:
    with pytest.raises(ValueError, match="EngineConfig.no_client_exit_s"):
        replace(ENGINE, no_client_exit_s=0.0)


def _python_engine(tmp_path: Path, body: str) -> EngineConfig:
    script = tmp_path / "engine.py"
    script.write_text(body, encoding="utf-8")
    return replace(ENGINE, exe=PYTHON, script=script)


def test_start_session_runs_engine_with_env_and_assigns_pid(tmp_path: Path) -> None:
    engine = _python_engine(
        tmp_path,
        "import os, sys\nopen(sys.argv[0] + '.out', 'w').write(os.environ['SESSION_ID'])\n",
    )
    assigned: list[int] = []
    process = start_session(
        assigned.append, os.environ, replace(ENDPOINT, log_path=tmp_path / "s1.log"), engine
    )
    assert process.wait(timeout=10) == 0
    assert assigned == [process.pid]
    assert (tmp_path / "engine.py.out").read_text() == "s1"


def test_start_session_sends_engine_output_to_the_log(tmp_path: Path) -> None:
    engine = _python_engine(
        tmp_path,
        "import sys\nprint('to-stdout', flush=True)\nprint('to-stderr', file=sys.stderr)\n",
    )
    log_path = tmp_path / "s1.log"
    log_path.write_text("earlier\n", encoding="utf-8")
    process = start_session(
        lambda pid: None, os.environ, replace(ENDPOINT, log_path=log_path), engine
    )
    process.wait(timeout=10)
    text = log_path.read_text(encoding="utf-8")
    assert text.startswith("earlier\n")
    assert "to-stdout" in text
    assert "to-stderr" in text


def test_start_session_kills_child_when_assign_fails(tmp_path: Path) -> None:
    engine = _python_engine(tmp_path, "import time\ntime.sleep(60)\n")
    children: list[psutil.Process] = []

    def refuse(pid: int) -> None:
        children.append(psutil.Process(pid))
        raise OSError("no job")

    with pytest.raises(OSError, match="no job"):
        start_session(refuse, os.environ, replace(ENDPOINT, log_path=tmp_path / "s.log"), engine)
    assert len(children) == 1
    child = children[0]
    try:
        child.wait(timeout=5)
    except psutil.TimeoutExpired:
        child.kill()
        pytest.fail("the engine process outlived the failed assign")
    assert not child.is_running()
