"""Run the Procfile's services as one unit: when any of them exits, all of them stop.

``scripts/start.ps1`` runs this in place of ``honcho start``, from the repo root:

    uv run python scripts/devstack.py [--procfile Procfile]

Why not honcho (2026-09-30): on Windows honcho stops a service by terminating only the
``cmd.exe`` it started, and counts the service as stopped only when its output pipe closes.
The ``uv -> python -> python`` chain under that shell survives and keeps the pipe open, so
honcho re-sends SIGKILL to the dead shell forever, never exits, and start.ps1's cleanup
never runs.

Here the supervisor joins a kill-on-close job before it starts anything, so every process a
service starts, however deep, is in the job too. Stopping the stack terminates the job,
which ends every process in it, this one included. If this process is killed instead, the
kernel closes the job's handle and the services die with it. A service counts as stopped
when its shell exits, not when its pipe closes.

Unlike honcho, this does not load ``.env`` into the services' environment: the settings
read it themselves, and honcho's parser dropped backslashes from Windows paths. Windows only,
like the job it relies on.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

from backend.playout.windows_job import KillOnCloseJob

_PROCFILE_LINE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.+)$")  # honcho's own pattern
_SYSTEM = "system"
_POLL_S = 0.2
# How long a stopped service's last lines (a startup traceback) may take to be relayed.
_DRAIN_S = 1.0
_CTRL_C_EXIT = 130
# As honcho: services get no console, so Ctrl+C reaches only the supervisor, which stops
# them all, and cmd.exe never asks "Terminate batch job (Y/N)?".
_CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class Service:
    name: str
    command: str


def parse_procfile(text: str) -> list[Service]:
    """The ``name: command`` lines of a Procfile; blank lines and ``#`` comments are skipped."""
    services: list[Service] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PROCFILE_LINE.match(line)
        if match is None:
            raise ValueError(f"Procfile line {number}: expected 'name: command', got {raw!r}")
        name, command = match.groups()
        if any(service.name == name for service in services):
            raise ValueError(f"Procfile line {number}: service {name!r} is defined twice")
        services.append(Service(name, command.strip()))
    if not services:
        raise ValueError("Procfile has no services")
    return services


class Console:
    """Writes ``HH:MM:SS name | line`` to an output, one whole line at a time."""

    def __init__(self, out: IO[str], width: int) -> None:
        self._out = out
        self._width = width
        self._lock = threading.Lock()

    def write(self, name: str, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._out.write(f"{stamp} {name:<{self._width}} | {text}\n")
            self._out.flush()


@dataclass(frozen=True)
class _Running:
    service: Service
    process: subprocess.Popen[bytes]
    relay: threading.Thread


def _relay(name: str, stream: IO[bytes], console: Console) -> None:
    for raw in iter(stream.readline, b""):
        console.write(name, raw.decode("utf-8", errors="replace").rstrip("\r\n"))


def _start(service: Service, console: Console) -> _Running:
    process = subprocess.Popen(
        service.command,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=_CREATE_NO_WINDOW,
    )
    relay = threading.Thread(
        target=_relay, args=(service.name, process.stdout, console), daemon=True
    )
    relay.start()
    console.write(_SYSTEM, f"{service.name} started (pid={process.pid})")
    return _Running(service, process, relay)


def _first_to_exit(running: Sequence[_Running]) -> _Running:
    while True:
        for entry in running:
            if entry.process.poll() is not None:
                return entry
        time.sleep(_POLL_S)


def _joined_job() -> KillOnCloseJob:
    """A new kill-on-close job holding this process, so every service started later is in it."""
    job = KillOnCloseJob()
    try:
        job.assign(os.getpid())
    except OSError:
        job.close()  # nothing is assigned, so this ends no process
        raise
    return job


def _supervise(services: Sequence[Service], job: KillOnCloseJob, console: Console) -> int:
    """Start every service, wait for the first to exit, then end the job, this process too."""
    running = [_start(service, console) for service in services]
    try:
        stopped = _first_to_exit(running)
    except KeyboardInterrupt:
        console.write(_SYSTEM, "Ctrl+C received; stopping every service")
        job.terminate(_CTRL_C_EXIT)
        return _CTRL_C_EXIT
    stopped.relay.join(_DRAIN_S)
    code = stopped.process.returncode & 0xFFFFFFFF  # the job takes an unsigned exit code
    console.write(_SYSTEM, f"{stopped.service.name} exited (rc={code}); stopping every service")
    job.terminate(code)
    return code  # not reached: terminating the job ends this process


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--procfile", type=Path, default=Path("Procfile"))
    args = parser.parse_args(argv)
    # A character the output's encoding lacks must not kill a relay: its service would block
    # on a full pipe. (honcho died this way on Vite's banner.)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    try:
        # utf-8-sig: Notepad and PowerShell 5.1 start the file with a BOM.
        services = parse_procfile(args.procfile.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        print(f"[devstack] {error}", file=sys.stderr)
        return 1
    try:
        job = _joined_job()
    except OSError as error:
        print(f"[devstack] cannot put the stack in a kill-on-close job: {error}", file=sys.stderr)
        return 1
    width = max(len(_SYSTEM), *(len(service.name) for service in services))
    return _supervise(services, job, Console(sys.stdout, width))


if __name__ == "__main__":
    sys.exit(main())
