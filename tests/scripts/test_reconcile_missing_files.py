"""The backlog script's report, and a move whose successor vanished."""

from __future__ import annotations

import importlib
import io
import sys
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import psycopg
import pytest

from backend.domain.library import MissingFileMove, MissingFilePlan

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

    lines = script.format_report(plan)

    assert "missing rows:           5" in lines
    assert "  to fold in:           2" in lines
    assert "  ambiguous:            1" in lines
    assert "  no present copy:      2" in lines
    assert "  /o/b  ->  /n/b" in lines
    assert "  /o/c" in lines


class _Conn:
    def transaction(self) -> _Conn:
        return self

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return False


def test_a_move_whose_successor_vanished_is_skipped(
    script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _apply(move: MissingFileMove, _repos: object) -> None:
        calls.append(move.missing_path)
        if move.missing_path == "/o/a":
            raise psycopg.errors.ForeignKeyViolation("successor gone")

    monkeypatch.setattr(script, "apply_missing_file_move", _apply)
    plan = MissingFilePlan(
        moves=(_move("/o/a", "/n/a", "w1", "w1"), _move("/o/b", "/n/b", "w1", "w1")),
        ambiguous=(),
        unmatched=(),
    )

    folded, skipped = script.apply_plan(plan, _Conn(), object())

    assert (folded, skipped) == (1, 1)
    assert calls == ["/o/a", "/o/b"]


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
