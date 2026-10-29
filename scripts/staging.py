"""The staging stack (phase 0b SPEC 2026-10-28, S11): this branch's API and Huey worker on a
gate copy, so Lance can use the pages he must review before the feature's single merge.

    python scripts/staging.py up --dsn <copy> [--state <file>]
    python scripts/staging.py down [--state <file>]

Both processes carry S10's guards: ``DATABASE_URL`` the copy (a bench database, or nothing
starts), ``STREAM_ENABLED=false`` and their own Huey file (``PYTEST_XDIST_WORKER=gate``). The API
listens on port 8020, beside the dev stack's 8010. ``up`` records their process ids in the state
file; ``down`` stops exactly those processes, and their children.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_common  # noqa: E402

API_PORT = 8020
DEFAULT_STATE = gate_common.ROOT / "audit" / "snapshots" / "staging.json"
API = ["-m", "backend.run_server"]
WORKER = ["-m", "huey.bin.huey_consumer", "backend.tasks.huey_app.huey", "-w", "1"]
_STACK_MODULES = ("backend.run_server", "huey.bin.huey_consumer")


def _guarded_env(dsn: str) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": dsn,
        "STREAM_ENABLED": "false",
        "PYTEST_XDIST_WORKER": "gate",
    }


def up(dsn: str, state: Path) -> None:
    name = gate_common.require_bench(dsn)
    if state.exists():
        raise SystemExit(f"a staging stack is already up ({state}): run `staging.py down` first")
    env = _guarded_env(dsn)
    root = gate_common.ROOT
    api = subprocess.Popen(
        [sys.executable, *API], cwd=root, env={**env, "SERVER_PORT": str(API_PORT)}
    )
    worker = subprocess.Popen([sys.executable, *WORKER], cwd=root, env=env)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"pids": [api.pid, worker.pid], "dsn_name": name}, indent=1) + "\n",
        encoding="utf-8",
    )
    print(
        f"staging up on {name}: API http://127.0.0.1:{API_PORT} (pid {api.pid}), "
        f"worker pid {worker.pid}"
    )


def stop_process_tree(pid: int) -> None:
    """Stop *pid* and its children, if it is still one of the stack's processes: a pid the
    system has given to another program since is left alone."""
    import psutil

    try:
        process = psutil.Process(pid)
        command = " ".join(process.cmdline())
    except psutil.NoSuchProcess:
        return
    if not any(module in command for module in _STACK_MODULES):
        print(f"pid {pid} is no longer a staging process; left alone")
        return
    tree = [*process.children(recursive=True), process]
    for member in tree:
        member.terminate()
    _gone, alive = psutil.wait_procs(tree, timeout=10)
    for member in alive:
        member.kill()


def down(state: Path) -> None:
    if not state.exists():
        print("no staging stack is up")
        return
    recorded = json.loads(state.read_text(encoding="utf-8"))
    for pid in recorded["pids"]:
        stop_process_tree(int(pid))
    state.unlink()
    print(f"staging down ({recorded['dsn_name']})")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    up_cmd = commands.add_parser("up", help="start the API and worker on a gate copy")
    up_cmd.add_argument("--dsn", required=True)
    up_cmd.add_argument("--state", type=Path, default=DEFAULT_STATE)
    down_cmd = commands.add_parser("down", help="stop what up started")
    down_cmd.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)
    if args.command == "up":
        up(args.dsn, args.state)
    else:
        down(args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
