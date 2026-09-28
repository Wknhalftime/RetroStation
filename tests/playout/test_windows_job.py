"""Kill-on-close job: an API crash takes every Liquidsoap child with it (spec: Engine).

A process killed by KILL_ON_JOB_CLOSE exits with code 0 (measured), so these tests assert
that the process is gone, never its exit code.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import psutil
import pytest

windows_job = pytest.importorskip("backend.playout.windows_job", reason="Windows only")
KillOnCloseJob = windows_job.KillOnCloseJob

# The venv's python.exe is a launcher that runs the real interpreter as a child, so a job
# holding the launcher would miss it. Use the real interpreter.
PYTHON = getattr(sys, "_base_executable", sys.executable)
SLEEPER = [PYTHON, "-c", "import time; time.sleep(60)"]
REPO_ROOT = Path(__file__).resolve().parents[2]

PARENT = textwrap.dedent(
    """
    import subprocess, sys, time
    from backend.playout.windows_job import KillOnCloseJob
    job = KillOnCloseJob()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    job.assign(child.pid)
    print(child.pid, flush=True)
    time.sleep(60)
    """
)


def _gone_within(process: psutil.Process, seconds: float) -> bool:
    try:
        process.wait(timeout=seconds)
    except psutil.TimeoutExpired:
        process.kill()
        return False
    return not process.is_running()


@pytest.mark.slow
def test_closing_job_kills_child() -> None:
    child = subprocess.Popen(SLEEPER)
    job = KillOnCloseJob()
    job.assign(child.pid)
    job.close()
    child.wait(timeout=5)


@pytest.mark.slow
def test_context_manager_closes_job() -> None:
    child = subprocess.Popen(SLEEPER)
    with KillOnCloseJob() as job:
        job.assign(child.pid)
    child.wait(timeout=5)


@pytest.mark.slow
def test_parent_death_kills_assigned_children() -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    parent = subprocess.Popen(
        [PYTHON, "-c", PARENT], stdout=subprocess.PIPE, text=True, cwd=REPO_ROOT, env=env
    )
    assert parent.stdout is not None
    grandchild = psutil.Process(int(parent.stdout.readline()))
    parent.kill()  # TerminateProcess: no cleanup code runs in the parent
    parent.wait(timeout=5)
    assert _gone_within(grandchild, 5)


def test_close_twice_is_harmless() -> None:
    job = KillOnCloseJob()
    job.close()
    job.close()


def test_assign_unknown_pid_raises() -> None:
    with KillOnCloseJob() as job, pytest.raises(OSError):
        job.assign(0)


def test_assign_after_close_raises() -> None:
    job = KillOnCloseJob()
    job.close()
    with pytest.raises(OSError, match="closed"):
        job.assign(1)
