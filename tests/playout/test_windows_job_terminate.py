"""KillOnCloseJob.terminate: end every process in the job now, with a chosen exit code.

The dev-stack supervisor (scripts/devstack.py) is in its own job and ends with it, so the
code it passes here is the code the stack exits with.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

windows_job = pytest.importorskip("backend.playout.windows_job", reason="Windows only")
KillOnCloseJob = windows_job.KillOnCloseJob

# The venv's python.exe is a launcher; a job holding the launcher would miss the interpreter.
PYTHON = getattr(sys, "_base_executable", sys.executable)
SLEEPER = [PYTHON, "-c", "import time; time.sleep(60)"]


@pytest.mark.slow
def test_terminate_ends_assigned_processes_with_the_exit_code() -> None:
    child = subprocess.Popen(SLEEPER)
    with KillOnCloseJob() as job:
        job.assign(child.pid)
        job.terminate(7)
        assert child.wait(timeout=5) == 7


def test_terminate_after_close_raises() -> None:
    job = KillOnCloseJob()
    job.close()
    with pytest.raises(OSError, match="closed"):
        job.terminate(1)
