"""Per-session engine logs (D35: "at app start, keep the newest 50")."""

from __future__ import annotations

import os
from pathlib import Path

from backend.playout.liquidsoap_process import prune_session_logs


def test_prune_keeps_the_newest_50_session_logs(tmp_path: Path) -> None:
    # Age runs opposite to the names (s00 is the newest), so keeping by name would fail.
    for i in range(55):
        log = tmp_path / f"s{i:02d}.log"
        log.write_text("engine output", encoding="utf-8")
        mtime = 1_000_000 + (54 - i)
        os.utime(log, (mtime, mtime))
    (tmp_path / "notes.txt").write_text("not a session log", encoding="utf-8")
    prune_session_logs(tmp_path)
    kept = sorted(log.name for log in tmp_path.glob("*.log"))
    assert kept == [f"s{i:02d}.log" for i in range(50)]
    assert (tmp_path / "notes.txt").exists()


def test_prune_is_harmless_on_a_missing_or_small_folder(tmp_path: Path) -> None:
    prune_session_logs(tmp_path / "not-created-yet")
    (tmp_path / "only.log").write_text("x", encoding="utf-8")
    prune_session_logs(tmp_path)
    assert (tmp_path / "only.log").exists()
