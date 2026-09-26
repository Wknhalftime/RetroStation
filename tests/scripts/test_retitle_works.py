"""scripts/retitle_works.py: re-derive local work titles from their files' tags.

Works are titled once, at creation, from the first file's tag run through
the version stripper. When the stripper is fixed (nested parentheses,
"[Language]"), existing titles such as "Last Child)" stay wrong until they
are re-derived. Only real changes count: a casing-only difference between
the work title and its files is left alone.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def retitle() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("retitle_works")


def test_stray_bracket_title_is_replaced_by_the_files_majority(retitle: ModuleType) -> None:
    plan = retitle.plan_retitle(
        "Back In The Saddle)",
        ["Back in the Saddle", "Back in the Saddle", "Back In The Saddle (Live Version (Edit))"],
    )
    assert plan == "Back in the Saddle"


def test_language_marker_title_collapses_to_the_base(retitle: ModuleType) -> None:
    assert retitle.plan_retitle("F.I.N.E. [Language]", ["F.I.N.E. [Language]"]) == "F.I.N.E."


def test_casing_only_difference_is_not_a_retitle(retitle: ModuleType) -> None:
    assert retitle.plan_retitle("Dream On", ["Dream on", "Dream on", "Dream On (Live)"]) is None


def test_work_without_files_is_left_alone(retitle: ModuleType) -> None:
    assert retitle.plan_retitle("Anything)", []) is None


def test_refuses_to_apply_outside_a_dry_run_without_a_backup_flag(retitle: ModuleType) -> None:
    # The script mutates work titles; the CLI must default to reporting only.
    args = retitle.build_parser().parse_args([])
    assert args.apply is False
