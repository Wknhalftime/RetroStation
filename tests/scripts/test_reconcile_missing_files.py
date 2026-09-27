"""The backlog script's dry-run report and its --apply summary."""

from __future__ import annotations

import importlib
import io
import sys
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest

from backend.domain.library import (
    MissingFileMove,
    MissingFilePlan,
    MissingFileReconciliation,
)

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("reconcile_missing_files")


def _move(old: str, new: str, old_work: str, new_work: str) -> MissingFileMove:
    return MissingFileMove(uuid4(), old, old_work, uuid4(), new, new_work)


def test_report_counts_and_lists_cross_work_and_ambiguous_rows(script: ModuleType) -> None:
    plan = MissingFilePlan(
        moves=(_move("/o/a", "/n/a", "w1", "w1"), _move("/o/b", "/n/b", "w1", "w2")),
        ambiguous=("/o/c",),
        unmatched=("/o/d", "/o/e"),
    )

    lines = script.format_report(plan, masters_to_repick=3)

    assert "missing rows:           5" in lines
    assert "  to fold in:           2" in lines
    assert "  ambiguous:            1" in lines
    assert "  no present copy:      2" in lines
    assert "masters to re-pick:     3" in lines
    assert "  /o/b  ->  /n/b" in lines
    assert "  /o/c" in lines


def test_apply_reports_folded_failed_and_repicked(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, object]] = []

    def _reconcile(conn: object, factory: object) -> MissingFileReconciliation:
        calls.append((conn, factory))
        return MissingFileReconciliation(
            reconciled=12, failed=2, ambiguous=4, unmatched=7, masters_repicked=5
        )

    monkeypatch.setattr(script, "reconcile_missing_after_scan", _reconcile)
    conn, factory = object(), object()

    line = script.apply_and_summarise(conn, factory)

    assert calls == [(conn, factory)]
    assert line == (
        "folded 12, failed 2, masters re-picked 5 "
        "(failed folds are logged as missing_file_fold_failed)"
    )


def test_apply_reports_a_run_that_stopped_early(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(script, "reconcile_missing_after_scan", lambda _c, _f: None)

    line = script.apply_and_summarise(object(), object())

    assert line == "reconciliation stopped early; see missing_reconciliation_failed in the log"


def test_use_utf8_console_lets_a_cp1252_console_print_non_ascii_paths(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = io.BytesIO()
    console = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", console)
    monkeypatch.setattr(sys, "stderr", console)

    script._use_utf8_console()
    report_line = "  Ol’ Dirty Bastard - Is This Real¿.flac"
    print(report_line)
    sys.stdout.flush()

    assert buffer.getvalue().decode("utf-8").rstrip("\r\n") == report_line
