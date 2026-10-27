"""R17's clipping guard (Lance, 2026-10-26): on the worst transients, at least 95% of the MP3
encoder's states decode at or below -1.0 dBTP and none above -0.5 dBTP; a chain preset's guard
record names the files it was measured with."""

from __future__ import annotations

from pathlib import Path

import pytest
from processor_harness import clipping_guard
from processor_harness.clipping_guard import Guard, GuardRecord, JudgedBlock

R17 = Guard(limit_dbtp=-1.0, share=0.95, clip_dbtp=-0.5)


def test_exactly_95_percent_at_or_below_the_limit_passes() -> None:
    verdict = clipping_guard.judge(R17, [-1.2] * 19 + [-0.8])

    assert verdict.passed
    assert verdict.share_at_or_below == pytest.approx(0.95)
    assert verdict.worst_dbtp == pytest.approx(-0.8)


def test_a_state_exactly_at_the_limit_counts_as_at_or_below() -> None:
    assert clipping_guard.judge(R17, [-1.0] * 20).share_at_or_below == 1.0


def test_under_95_percent_at_or_below_the_limit_fails() -> None:
    verdict = clipping_guard.judge(R17, [-1.2] * 94 + [-0.9] * 6)

    assert not verdict.passed
    assert verdict.share_at_or_below == pytest.approx(0.94)


def test_one_state_over_the_clipping_guard_fails_however_rare() -> None:
    verdict = clipping_guard.judge(R17, [-1.2] * 999 + [-0.49])

    assert not verdict.passed
    assert verdict.share_at_or_below == pytest.approx(0.999)


def make_chain(root: Path) -> Path:
    """A preset whose chain includes a library file, as full_chain.liq does."""
    (root / "chains" / "lib").mkdir(parents=True)
    (root / "chains" / "lib" / "limiter.liq").write_text("def limit(x) = x end\n", encoding="utf-8")
    (root / "chains" / "chain.liq").write_text(
        '%include "lib/limiter.liq"\ndef chain(s) = limit(s) end\n', encoding="utf-8"
    )
    (root / "presets").mkdir()
    preset = root / "presets" / "chain-x.toml"
    preset.write_text(
        'id = "chain-x"\nversion = 1\nbackend = "liquidsoap"\nchain = "../chains/chain.liq"\n'
        "[params]\nlimit_db = -2.5\n",
        encoding="utf-8",
    )
    return preset


def test_the_fingerprint_changes_when_an_included_script_changes(tmp_path: Path) -> None:
    preset = make_chain(tmp_path)
    before = clipping_guard.fingerprint(preset)

    (tmp_path / "chains" / "lib" / "limiter.liq").write_text("def limit(x) = x end # ed\n")

    assert clipping_guard.fingerprint(preset) != before


def test_the_fingerprint_changes_when_the_preset_changes(tmp_path: Path) -> None:
    preset = make_chain(tmp_path)
    before = clipping_guard.fingerprint(preset)

    preset.write_text(preset.read_text(encoding="utf-8").replace("-2.5", "-2.0"), encoding="utf-8")

    assert clipping_guard.fingerprint(preset) != before


def test_the_fingerprint_ignores_line_endings(tmp_path: Path) -> None:
    # Git checks the files out with CRLF on Windows and LF on the CI runner.
    preset = make_chain(tmp_path)
    files = [preset, *tmp_path.glob("chains/**/*.liq")]

    def with_endings(ending: bytes) -> str:
        for path in files:
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", ending))
        return clipping_guard.fingerprint(preset)

    assert with_endings(b"\n") == with_endings(b"\r\n")


def test_a_record_reads_back_as_written(tmp_path: Path) -> None:
    preset = make_chain(tmp_path)
    record = GuardRecord(
        preset="chain-x",
        fingerprint=clipping_guard.fingerprint(preset),
        guard=R17,
        lead_ins=1152,
        run="2026-10-26-r17-guard",
        passed=True,
        blocks=(JudgedBlock("hotel-california", 129.2, -0.857, 0.998, -1.795, 2, True),),
    )

    clipping_guard.write_record(preset, record)

    assert clipping_guard.record_path(preset) == tmp_path / "presets" / "chain-x.guard.json"
    assert clipping_guard.read_record(preset) == record


def test_a_missing_record_reads_as_none(tmp_path: Path) -> None:
    assert clipping_guard.read_record(make_chain(tmp_path)) is None
