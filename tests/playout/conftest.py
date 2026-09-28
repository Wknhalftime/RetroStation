from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.playout.liquidsoap_process import SESSION_SCRIPT, EngineConfig, warm_script_cache

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_CACHE_BUILD_TIMEOUT_S = 180.0


@pytest.fixture(scope="session")
def liquidsoap_exe() -> Path:
    raw = os.environ.get("LIQUIDSOAP_PATH")
    if not raw or not Path(raw).is_file():
        pytest.skip("LIQUIDSOAP_PATH not set to an installed Liquidsoap")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not on PATH")
    return Path(raw)


def _wait_for_other_worker(status: Path) -> None:
    deadline = time.monotonic() + _CACHE_BUILD_TIMEOUT_S
    while not status.exists():
        if time.monotonic() > deadline:
            pytest.fail("another worker did not finish warming the Liquidsoap cache")
        time.sleep(0.2)
    if status.read_text(encoding="utf-8") != "ok":
        pytest.fail("another worker failed to warm the Liquidsoap cache")


def _publish(status: Path, text: str) -> None:
    staged = status.with_suffix(".tmp")
    staged.write_text(text, encoding="utf-8")
    os.replace(staged, status)  # atomic: a waiting worker never reads a partial status


@pytest.fixture(scope="session")
def liq_cache(
    liquidsoap_exe: Path, tmp_path_factory: pytest.TempPathFactory, worker_id: str
) -> Path:
    """A script cache warmed once per run and shared by every xdist worker.

    A cold cache costs about 5 s per process (spike), so without it every session test
    would measure Liquidsoap's type-checker instead of the script.
    """
    root = tmp_path_factory.getbasetemp()
    if worker_id != "master":
        root = root.parent  # shared by all workers of this run
    cache = root / "liq-cache"
    status = root / "liq-cache.status"
    try:
        fd = os.open(root / "liq-cache.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        _wait_for_other_worker(status)
        return cache
    os.close(fd)
    cache.mkdir(parents=True, exist_ok=True)
    engine = EngineConfig(
        exe=liquidsoap_exe,
        script=SESSION_SCRIPT,
        cache_dir=cache,
        filler=cache / "unused.flac",
        intro_sfx=None,
    )
    try:
        warm_script_cache(os.environ, engine)
    except (subprocess.SubprocessError, OSError):
        _publish(status, "failed")
        raise
    _publish(status, "ok")
    return cache
