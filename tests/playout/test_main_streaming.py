"""The composition root builds streaming only when enabled and able (spec: Engine "the app
must warm it at startup"; D34 a cache that cannot be built turns streaming off; D35 logs
pruned at app start; D46 pruning never stops startup; review I5: streaming is opt-in)."""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from backend.config import Settings
from backend.main import start_streaming, stop_streaming

NOT_LIQUIDSOAP = Path(getattr(sys, "_base_executable", sys.executable))  # rejects --cache-only
if re.search(r"\\\d", str(NOT_LIQUIDSOAP)):
    pytest.skip("the interpreter path contains a backslash-digit", allow_module_level=True)


async def test_streaming_is_off_unless_enabled(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, liquidsoap_path=NOT_LIQUIDSOAP, stream_work_dir=tmp_path)
    assert await start_streaming(settings) is None
    assert not (tmp_path / "cache").exists()


async def test_enabled_without_liquidsoap_stays_off_and_says_why(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, stream_enabled=True, liquidsoap_path=None, stream_work_dir=tmp_path
    )
    with capture_logs() as logs:
        assert await start_streaming(settings) is None
    assert any(e["event"] == "stream_engine_unavailable" for e in logs)


@pytest.mark.skipif(sys.platform != "win32", reason="streaming runs on Windows only (D5)")
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg builds the noise assets")
async def test_a_script_cache_that_cannot_be_built_turns_streaming_off(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        stream_enabled=True,
        liquidsoap_path=NOT_LIQUIDSOAP,
        stream_work_dir=tmp_path,
    )
    with capture_logs() as logs:
        runtime = await start_streaming(settings)
    assert runtime is None
    [entry] = [e for e in logs if e["event"] == "stream_engine_unavailable"]
    assert entry["log_level"] == "error"
    # What the stand-in printed on stderr ("unknown option --cache-only ..."); the command
    # line alone never contains these words.
    assert "unknown option" in str(entry["output"])


@pytest.mark.slow
@pytest.mark.timeout(180)
async def test_startup_warms_the_cache_and_keeps_the_newest_50_logs(
    tmp_path: Path, liquidsoap_exe: Path
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    for i in range(52):
        (logs_dir / f"old{i:02d}.log").write_text("x", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        stream_enabled=True,
        liquidsoap_path=liquidsoap_exe,
        stream_work_dir=tmp_path,
    )
    runtime = await start_streaming(settings)
    try:
        assert runtime is not None
        assert any((tmp_path / "cache").iterdir())
        assert len(list(logs_dir.glob("*.log"))) == 50
        assert runtime.service.open_sessions == 0
    finally:
        await stop_streaming(runtime)


@pytest.mark.slow
@pytest.mark.timeout(180)
@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to delete an open file")
async def test_a_log_that_cannot_be_pruned_does_not_stop_startup(
    tmp_path: Path, liquidsoap_exe: Path
) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    for i in range(52):
        log = logs_dir / f"old{i:02d}.log"
        log.write_text("x", encoding="utf-8")
        os.utime(log, (1_000_000 + i, 1_000_000 + i))  # old00 is the oldest
    settings = Settings(
        _env_file=None,
        stream_enabled=True,
        liquidsoap_path=liquidsoap_exe,
        stream_work_dir=tmp_path,
    )
    held = logs_dir / "old00.log"
    with held.open("rb"), capture_logs() as logs:  # another process still has it open
        runtime = await start_streaming(settings)
    try:
        assert runtime is not None  # D46: startup went on, and streaming is on
        assert any(e["event"] == "stream_log_prune_failed" and "old00" in str(e) for e in logs)
        assert sorted(p.name for p in logs_dir.glob("old*.log"))[-50:] == [
            f"old{i:02d}.log" for i in range(2, 52)
        ]
    finally:
        await stop_streaming(runtime)
