"""Phase 0b gate (SPEC 2026-10-28): the staging stack (S11).

``scripts/staging.py up --dsn <copy> [--state <file>]`` starts this branch's API (port 8020) and
Huey worker on a gate copy, each with S10's guards: ``DATABASE_URL`` the copy,
``STREAM_ENABLED=false`` and its own Huey file (``PYTEST_XDIST_WORKER=gate``). It records their
process ids in the state file; ``down [--state <file>]`` stops exactly those processes.
No process is started here: ``subprocess.Popen`` and the stopping are replaced. Each test
restores the process environment and the settings cache afterwards (``process_env``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.scripts.gate_world import load_tool, read_json

COPY = "postgresql://retrostation:secret@localhost:5432/retrostation_bench_gate_feat_x"
API = ["-m", "backend.run_server"]
WORKER = ["-m", "huey.bin.huey_consumer", "backend.tasks.huey_app.huey", "-w", "1"]

pytestmark = pytest.mark.usefixtures("process_env")


def runs(args: list[str], command: list[str]) -> bool:
    """Whether *command* appears in *args* as one run of consecutive items (so it may be
    launched directly or through a wrapper such as ``uv run python``)."""
    return any(args[i : i + len(command)] == command for i in range(len(args)))


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid


@pytest.fixture(scope="module")
def staging() -> Any:
    return load_tool("staging")


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch, staging: Any) -> list[dict[str, Any]]:
    """What staging asked ``subprocess.Popen`` to start: args and env, one fake pid each."""
    calls: list[dict[str, Any]] = []

    def popen(args: Any, **kwargs: Any) -> FakeProcess:
        calls.append({"args": [str(a) for a in args], "env": dict(kwargs["env"])})
        return FakeProcess(41000 + len(calls))

    monkeypatch.setattr(staging.subprocess, "Popen", popen)
    return calls


@pytest.fixture
def stopped(monkeypatch: pytest.MonkeyPatch, staging: Any) -> list[int]:
    pids: list[int] = []
    monkeypatch.setattr(staging, "stop_process_tree", pids.append)
    return pids


def test_refuses_a_non_bench_dsn(
    staging: Any, started: list[dict[str, Any]], tmp_path: Path
) -> None:
    """S11, S10: a copy without "bench" in its name is refused; nothing starts."""
    dev = "postgresql://retrostation:secret@localhost:5432/retrostation"
    with pytest.raises(SystemExit, match="bench"):
        staging.main(["up", "--dsn", dev, "--state", str(tmp_path / "staging.json")])
    assert started == []
    assert not (tmp_path / "staging.json").exists()


def test_up_starts_api_and_worker_on_the_copy_and_never_streams(
    staging: Any,
    started: list[dict[str, Any]],
    stopped: list[int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S11: the API on port 8020 and one Huey worker, both on the copy, streaming off (whatever
    the caller's environment says) and on the gate's own Huey file; down stops exactly them."""
    monkeypatch.setenv("STREAM_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://retrostation:x@localhost:5432/retrostation")
    state = tmp_path / "staging.json"

    assert staging.main(["up", "--dsn", COPY, "--state", str(state)]) == 0

    assert len(started) == 2
    [api] = [p for p in started if runs(p["args"], API)]
    [worker] = [p for p in started if runs(p["args"], WORKER)]
    assert api is not worker
    for process in started:
        env = process["env"]
        assert env["DATABASE_URL"] == COPY
        assert env["STREAM_ENABLED"] == "false"
        assert env["PYTEST_XDIST_WORKER"] == "gate"
    assert api["env"]["SERVER_PORT"] == "8020"
    recorded = read_json(state)
    assert sorted(recorded["pids"]) == [41001, 41002]
    assert recorded["dsn_name"] == "retrostation_bench_gate_feat_x"

    with pytest.raises(SystemExit):  # already up: a second stack is refused
        staging.main(["up", "--dsn", COPY, "--state", str(state)])
    assert len(started) == 2

    assert staging.main(["down", "--state", str(state)]) == 0
    assert sorted(stopped) == [41001, 41002]
    assert not state.exists()


def test_down_with_nothing_up(staging: Any, stopped: list[int], tmp_path: Path) -> None:
    """S11: down with no stack recorded stops nothing."""
    assert staging.main(["down", "--state", str(tmp_path / "staging.json")]) == 0
    assert stopped == []
