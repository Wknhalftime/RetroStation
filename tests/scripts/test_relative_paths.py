"""scripts/audit/relative_paths.py makes ruff's JSON report checkout-independent.

ruff writes absolute filenames, so an audit run from a worktree
(D:/PythonStuff/RetroStation-audit1/...) and one run from the main checkout differ
on every finding. Committed audits are diffed as the trend report; paths must be
repo-relative for that diff to show only real changes.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "audit"


@pytest.fixture(scope="module")
def relative_paths() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("relative_paths")


def test_filenames_under_root_become_relative_posix(
    relative_paths: ModuleType, tmp_path: Path
) -> None:
    findings = [
        {"code": "S105", "filename": str(tmp_path / "backend" / "config.py")},
        {"code": "T201", "filename": str(tmp_path / "scripts" / "audit" / "hotspots.py")},
    ]
    result = relative_paths.relativize(findings, tmp_path)
    assert [f["filename"] for f in result] == ["backend/config.py", "scripts/audit/hotspots.py"]
    assert [f["code"] for f in result] == ["S105", "T201"]


def test_filename_outside_root_is_left_alone(relative_paths: ModuleType, tmp_path: Path) -> None:
    outside = str(tmp_path.parent / "elsewhere.py")
    result = relative_paths.relativize([{"filename": outside}], tmp_path)
    assert result == [{"filename": outside}]


def test_main_rewrites_the_report_in_place(
    relative_paths: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "ruff.json"
    report.write_text(
        json.dumps([{"filename": str(tmp_path / "backend" / "main.py")}]), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    relative_paths.main([str(report)])
    assert json.loads(report.read_text(encoding="utf-8")) == [{"filename": "backend/main.py"}]
