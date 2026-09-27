"""scripts/audit/hotspots.py must not count formatting-only commits as churn.

A repo-wide `ruff format` commit touches ~190 files; counted as a change it adds
+1 to every file's churn and skews each audit's trend diff. The commits to skip are
the ones git blame already skips: those listed in .git-blame-ignore-revs.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "audit"

FORMAT_REV = "ffdefef6b048352004901db1900da1d38b6826dd"
OTHER_REV = "0ca84e3aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

# `git log --name-only --format=format:@@commit %H` output: two commits, the first a
# formatting sweep.
LOG = (
    f"@@commit {FORMAT_REV}\n"
    "backend/a.py\nbackend/b.py\n\n"
    f"@@commit {OTHER_REV}\n"
    "backend/a.py\nbackend/notes.md\n"
)


@pytest.fixture(scope="module")
def hotspots() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("hotspots")


def test_ignored_revs_reads_hashes_and_skips_comments(hotspots: ModuleType, tmp_path: Path) -> None:
    revs = tmp_path / ".git-blame-ignore-revs"
    revs.write_text(
        f"# Commits that only reformat code.\n\n# style: ruff format\n{FORMAT_REV}\n",
        encoding="utf-8",
    )
    assert hotspots.ignored_revs(revs) == {FORMAT_REV}


def test_missing_ignore_file_skips_nothing(hotspots: ModuleType, tmp_path: Path) -> None:
    assert hotspots.ignored_revs(tmp_path / "absent") == set()


def test_parse_log_drops_ignored_commits(hotspots: ModuleType) -> None:
    commits, skipped = hotspots.parse_log(LOG, {FORMAT_REV})
    assert commits == [["backend/a.py"]]
    assert skipped == 1


def test_parse_log_matches_abbreviated_ignored_hash(hotspots: ModuleType) -> None:
    commits, skipped = hotspots.parse_log(LOG, {FORMAT_REV[:7]})
    assert commits == [["backend/a.py"]]
    assert skipped == 1


def test_parse_log_keeps_everything_without_ignores(hotspots: ModuleType) -> None:
    commits, skipped = hotspots.parse_log(LOG, set())
    assert commits == [["backend/a.py", "backend/b.py"], ["backend/a.py"]]
    assert skipped == 0
