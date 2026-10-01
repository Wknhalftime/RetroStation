"""scripts/devstack.py: when any service exits, the whole stack stops and nothing outlives it.

honcho stopped a service by killing only its cmd.exe; the uv -> python chain under it kept
running and held the output pipe open, so honcho never saw the service stop and re-sent
SIGKILL to the dead shell forever (2026-09-30). These tests start services that leave a
grandchild behind, the shape of that chain, and assert the grandchild is gone too.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import psutil
import pytest

pytest.importorskip("backend.playout.windows_job", reason="Windows only")

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = REPO_ROOT / "scripts"
DEVSTACK = _SCRIPTS / "devstack.py"
# The venv's python.exe is a launcher; run the real interpreter so the PIDs are the real ones.
PYTHON = getattr(sys, "_base_executable", sys.executable)

# Starts a grandchild that inherits the output pipe, records its PID, then waits.
HOLDER = textwrap.dedent(
    """
    import subprocess, sys, time
    from pathlib import Path
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    partial = Path(sys.argv[1] + ".tmp")
    partial.write_text(str(child.pid))
    partial.replace(sys.argv[1])
    print("holder ready", flush=True)
    time.sleep(60)
    """
)

# Exits with code 3 once the holder's grandchild is running.
QUITTER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    while not Path(sys.argv[1]).exists():
        time.sleep(0.05)
    print("quitter leaving", flush=True)
    sys.exit(3)
    """
)


@pytest.fixture(scope="module")
def devstack() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("devstack")


@pytest.fixture
def pid_file(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "grandchild.pid"
    yield path
    if path.exists():  # a failed test must not leave its grandchild running
        with contextlib.suppress(psutil.NoSuchProcess):
            psutil.Process(int(path.read_text())).kill()


def _write_procfile(tmp_path: Path, services: dict[str, str], pid_file: Path) -> Path:
    lines = []
    for name, source in services.items():
        script = tmp_path / f"{name}.py"
        script.write_text(source)
        lines.append(f'{name}: "{PYTHON}" "{script}" "{pid_file}"')
    procfile = tmp_path / "Procfile"
    procfile.write_text("\n".join(lines) + "\n")
    return procfile


def _devstack_command(procfile: Path) -> list[str]:
    return [PYTHON, str(DEVSTACK), "--procfile", str(procfile)]


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONUTF8": "1"}


def _wait_for(path: Path, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.05)


def test_parse_procfile_reads_name_and_command(devstack: ModuleType) -> None:
    text = "# dev stack\n\napi:    uv run python -m backend.run_server\nweb: cd a && b\n"

    services = devstack.parse_procfile(text)

    assert [(s.name, s.command) for s in services] == [
        ("api", "uv run python -m backend.run_server"),
        ("web", "cd a && b"),
    ]


def test_parse_procfile_rejects_a_line_without_a_name(devstack: ModuleType) -> None:
    with pytest.raises(ValueError, match="line 1"):
        devstack.parse_procfile("uv run python -m backend.run_server\n")


def test_parse_procfile_rejects_a_duplicate_name(devstack: ModuleType) -> None:
    with pytest.raises(ValueError, match="api"):
        devstack.parse_procfile("api: a\napi: b\n")


def test_parse_procfile_rejects_an_empty_file(devstack: ModuleType) -> None:
    with pytest.raises(ValueError, match="no services"):
        devstack.parse_procfile("# nothing here\n")


@pytest.mark.slow
def test_a_service_exit_stops_every_process_in_the_stack(tmp_path: Path, pid_file: Path) -> None:
    procfile = _write_procfile(tmp_path, {"holder": HOLDER, "quitter": QUITTER}, pid_file)

    result = subprocess.run(
        _devstack_command(procfile),
        cwd=REPO_ROOT,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert "quitter exited (rc=3)" in result.stdout
    assert "quitter leaving" in result.stdout  # its last words are relayed before the stop
    assert not psutil.pid_exists(int(pid_file.read_text()))


@pytest.mark.slow
def test_killing_the_supervisor_kills_every_service(tmp_path: Path, pid_file: Path) -> None:
    procfile = _write_procfile(tmp_path, {"holder": HOLDER}, pid_file)
    supervisor = subprocess.Popen(
        _devstack_command(procfile),
        cwd=REPO_ROOT,
        env=_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for(pid_file, 20)
        grandchild = psutil.Process(int(pid_file.read_text()))
    finally:
        supervisor.kill()  # TerminateProcess: no cleanup code runs in the supervisor
        supervisor.wait(timeout=5)

    try:
        grandchild.wait(timeout=5)
    except psutil.TimeoutExpired:
        pytest.fail("a service outlived its supervisor")
