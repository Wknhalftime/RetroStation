"""Pruning per-session engine logs tolerates the folder changing under it (D35: "at app
start, keep the newest 50"; D46: a log that vanishes, is in use, or is pending deletion is
tolerated). Extends the locked tests/playout/test_session_logs.py.

Each case is a real file-system state, not a patched call: a log whose target is gone, a log
another process holds open, and a second pruner deleting the same logs at the same time."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from backend.playout.liquidsoap_process import prune_session_logs

KEEP = 50


def logs_newest_first(folder: Path, count: int, first: int = 0) -> list[Path]:
    """``count`` logs named from ``first`` on; the lowest number is the newest."""
    logs = []
    for i in range(count):
        log = folder / f"s{first + i:03d}.log"
        log.write_text("engine output", encoding="utf-8")
        mtime = 1_000_000 + (count - 1 - i)
        os.utime(log, (mtime, mtime))
        logs.append(log)
    return logs


def real_logs(folder: Path) -> list[str]:
    """The names of the logs that are regular files (links are not session logs)."""
    return sorted(p.name for p in folder.glob("*.log") if p.is_file() and not p.is_symlink())


def test_a_log_whose_file_is_gone_is_skipped(tmp_path: Path) -> None:
    logs = logs_newest_first(tmp_path, 55, first=1)
    dangling = tmp_path / "s000.log"
    try:
        os.symlink(tmp_path / "gone.bin", dangling)
    except OSError as refused:  # Windows without Developer Mode or the privilege
        pytest.skip(f"symbolic links are not permitted here: {refused}")
    prune_session_logs(tmp_path)
    assert real_logs(tmp_path) == sorted(log.name for log in logs[:KEEP])


def test_a_log_held_open_elsewhere_is_left_and_the_rest_are_pruned(tmp_path: Path) -> None:
    logs = logs_newest_first(tmp_path, 55)
    with logs[-1].open("rb"):  # the oldest: due for pruning, but another process has it open
        prune_session_logs(tmp_path)
    assert sorted(log.name for log in logs[:KEEP]) == real_logs(tmp_path)[:KEEP]
    assert all(not log.exists() for log in logs[KEEP:-1])


@pytest.mark.slow
def test_two_pruners_at_once_both_finish_and_keep_the_newest(tmp_path: Path) -> None:
    for round_ in range(20):
        folder = tmp_path / f"round{round_:02d}"
        folder.mkdir()
        logs = logs_newest_first(folder, 300)
        start = threading.Barrier(2)
        failures: list[OSError] = []

        def prune(
            folder: Path = folder,
            start: threading.Barrier = start,
            failures: list[OSError] = failures,
        ) -> None:
            start.wait()
            try:
                prune_session_logs(folder)
            except OSError as error:
                failures.append(error)

        pruners = [threading.Thread(target=prune) for _ in range(2)]
        for pruner in pruners:
            pruner.start()
        for pruner in pruners:
            pruner.join(timeout=30)
        assert failures == [], f"round {round_}: {failures[0]!r}"
        assert real_logs(folder) == sorted(log.name for log in logs[:KEEP])
