"""The Huey queue files resolve to one absolute path, whatever the working directory.

An API and a consumer started from different directories must share a queue; a relative
file name would give each its own, silently (PR E2 review, M3).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.tasks.huey_files import PROJECT_ROOT, huey_db_path

REPO = Path(__file__).resolve().parents[2]

PROBE = """
import json
import backend.tasks.cue_huey_app as cues
import backend.tasks.huey_app as library
print(json.dumps([cues.cue_huey.storage.filename, library.huey.storage.filename]))
"""


def test_project_root_is_the_repo() -> None:
    assert PROJECT_ROOT == REPO


@pytest.mark.parametrize("worker", [None, "gw3"])
def test_path_is_absolute_and_independent_of_cwd(
    worker: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if worker is None:
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    else:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)

    monkeypatch.chdir(REPO)
    from_repo = huey_db_path("huey_cues")
    monkeypatch.chdir(tmp_path)
    from_elsewhere = huey_db_path("huey_cues")

    assert Path(from_repo).is_absolute()
    assert from_repo == from_elsewhere
    expected = "huey_cues.db" if worker is None else f"huey_cues_{worker}.db"
    assert Path(from_repo) == REPO / expected


def _probe_files(cwd: Path) -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_XDIST_WORKER"}
    probe = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    files: list[str] = json.loads(probe.stdout.strip().splitlines()[-1])
    return files


@pytest.mark.slow
def test_both_queues_use_the_same_files_from_any_cwd(tmp_path: Path) -> None:
    """Both real queues, imported from the repo and from a foreign directory, agree."""
    from_repo = _probe_files(REPO)
    from_elsewhere = _probe_files(tmp_path)

    assert from_repo == from_elsewhere
    assert from_repo == [str(REPO / "huey_cues.db"), str(REPO / "huey.db")]
