"""The session job pins listener sessions (R18, Lance 2026-10-27): every engine assigned to it,
and anything an engine starts, runs on the job's CPUs at the job's priority class."""

from __future__ import annotations

import subprocess
import sys

import psutil
import pytest

windows_job = pytest.importorskip("backend.playout.windows_job", reason="Windows only")
KillOnCloseJob = windows_job.KillOnCloseJob

# The venv's python.exe is a launcher; the real interpreter is the process to watch.
PYTHON = getattr(sys, "_base_executable", sys.executable)
SLEEPER = [PYTHON, "-c", "import time; time.sleep(60)"]
LAST_TWO = tuple(range(psutil.cpu_count() or 2))[-2:]


@pytest.mark.slow
def test_an_assigned_process_runs_on_the_jobs_cpus_at_its_priority() -> None:
    child = subprocess.Popen(SLEEPER)
    with KillOnCloseJob(cpus=LAST_TWO, priority_class=windows_job.ABOVE_NORMAL) as job:
        job.assign(child.pid)
        process = psutil.Process(child.pid)

        assert tuple(process.cpu_affinity()) == LAST_TWO
        assert process.nice() == psutil.ABOVE_NORMAL_PRIORITY_CLASS
    child.wait(timeout=5)


@pytest.mark.slow
def test_a_process_an_engine_starts_gets_the_jobs_cpus_and_priority() -> None:
    starter = subprocess.Popen(
        [PYTHON, "-c", "import subprocess, sys, time; input(); "
         "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
         "print(c.pid, flush=True); time.sleep(60)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    with KillOnCloseJob(cpus=LAST_TWO, priority_class=windows_job.ABOVE_NORMAL) as job:
        job.assign(starter.pid)
        assert starter.stdin is not None and starter.stdout is not None
        starter.stdin.write("\n")
        starter.stdin.flush()  # the child starts only once the starter is in the job
        child = psutil.Process(int(starter.stdout.readline()))

        assert tuple(child.cpu_affinity()) == LAST_TWO
        assert child.nice() == psutil.ABOVE_NORMAL_PRIORITY_CLASS
    starter.wait(timeout=5)


@pytest.mark.slow
def test_a_job_without_cpus_or_priority_leaves_both_alone() -> None:
    child = subprocess.Popen(SLEEPER)
    with KillOnCloseJob() as job:
        before = psutil.Process(child.pid).cpu_affinity()
        job.assign(child.pid)

        assert psutil.Process(child.pid).cpu_affinity() == before
        assert psutil.Process(child.pid).nice() == psutil.NORMAL_PRIORITY_CLASS
    child.wait(timeout=5)
