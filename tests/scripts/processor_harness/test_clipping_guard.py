"""R17's clipping guard (Lance, 2026-10-26; guard amended 2026-10-27): on the worst transients,
at least 95% of the MP3 encoder's states decode at or below -1.0 dBTP and none above -0.3 dBTP; a
chain preset's guard record names the files it was measured with."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from processor_harness import clipping_guard, clipping_guard_cli
from processor_harness.clipping_guard import Guard, GuardRecord, JudgedBlock

GUARD = Guard(limit_dbtp=-1.0, share=0.95, clip_dbtp=-0.5)


def test_exactly_95_percent_at_or_below_the_limit_passes() -> None:
    verdict = clipping_guard.judge(GUARD, [-1.2] * 19 + [-0.8])

    assert verdict.passed
    assert verdict.share_at_or_below == pytest.approx(0.95)
    assert verdict.worst_dbtp == pytest.approx(-0.8)


def test_a_state_exactly_at_the_limit_counts_as_at_or_below() -> None:
    assert clipping_guard.judge(GUARD, [-1.0] * 20).share_at_or_below == 1.0


def test_under_95_percent_at_or_below_the_limit_fails() -> None:
    verdict = clipping_guard.judge(GUARD, [-1.2] * 94 + [-0.9] * 6)

    assert not verdict.passed
    assert verdict.share_at_or_below == pytest.approx(0.94)


def test_r17s_clipping_guard_is_minus_0_3_dbtp() -> None:
    # -0.5 was a round margin; -0.3 still keeps every state under the decoder's 0 dBFS clip.
    assert Guard(limit_dbtp=-1.0, share=0.95, clip_dbtp=-0.3) == clipping_guard.R17


def test_r17_passes_the_in_the_air_tonight_block_the_minus_0_5_guard_failed() -> None:
    states = [-1.5] * 1148 + [-0.9, -0.8, -0.6, -0.468]  # 223.1 s: 4 of 1152 over -1.0

    assert clipping_guard.judge(clipping_guard.R17, states).passed
    assert not clipping_guard.judge(clipping_guard.R17, states[:-1] + [-0.29]).passed


def test_one_state_over_the_clipping_guard_fails_however_rare() -> None:
    verdict = clipping_guard.judge(GUARD, [-1.2] * 999 + [-0.49])

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
        guard=GUARD,
        lead_ins=1152,
        run="2026-10-26-r17-guard",
        passed=True,
        blocks=(JudgedBlock("hotel-california", 129.2, -0.857, 0.998, -1.795, 2, True),),
    )

    clipping_guard.write_record(preset, record)

    assert clipping_guard.record_path(preset) == tmp_path / "presets" / "chain-x.guard.json"
    assert clipping_guard.read_record(preset) == record


def record_at(preset: Path, bitrate: str, worst_dbtp: float) -> GuardRecord:
    return GuardRecord(
        preset="chain-x",
        fingerprint=clipping_guard.fingerprint(preset),
        guard=GUARD,
        lead_ins=1152,
        run="2026-10-27-r17-guard",
        passed=True,
        blocks=(JudgedBlock("air-tonight", 223.1, worst_dbtp, 0.9965, -1.812, 4, True),),
        bitrate=bitrate,
    )


def test_records_at_two_bitrates_are_kept_side_by_side(tmp_path: Path) -> None:
    # A bitrate setting would need each bitrate's guard result (Lance, 2026-10-28), so a run at
    # one bitrate keeps the records at the others.
    preset = make_chain(tmp_path)
    at_128k, at_256k = record_at(preset, "128k", -0.468), record_at(preset, "256k", -0.6)

    clipping_guard.write_record(preset, at_128k)
    clipping_guard.write_record(preset, at_256k)

    assert clipping_guard.read_record(preset, "128k") == at_128k
    assert clipping_guard.read_record(preset, "256k") == at_256k
    assert clipping_guard.read_records(preset) == [at_128k, at_256k]


def test_a_record_reads_at_the_streams_bitrate_unless_told_otherwise(tmp_path: Path) -> None:
    preset = make_chain(tmp_path)
    clipping_guard.write_record(preset, record_at(preset, "128k", -0.468))

    assert clipping_guard.read_record(preset) is None  # the stream is at 256k (R20)


def test_a_guard_file_from_before_r20_reads_as_its_128k_record(tmp_path: Path) -> None:
    # Before R20 (2026-10-28) the file held one record, judged at the stream's 128k then.
    preset = make_chain(tmp_path)
    at_128k = record_at(preset, "128k", -0.468)
    flat = {k: v for k, v in dataclasses.asdict(at_128k).items() if k != "bitrate"}
    clipping_guard.record_path(preset).write_text(json.dumps(flat), encoding="utf-8")

    assert clipping_guard.read_record(preset, "128k") == at_128k


def test_a_missing_record_reads_as_none(tmp_path: Path) -> None:
    assert clipping_guard.read_record(make_chain(tmp_path)) is None


def failing_record(preset: Path, last_worst_dbtp: float) -> GuardRecord:
    """The shape of chain-r17's first record: one block over the -0.5 guard, the rest passing."""
    return GuardRecord(
        preset="chain-x",
        fingerprint=clipping_guard.fingerprint(preset),
        guard=GUARD,
        lead_ins=1152,
        run="2026-10-26-r17-guard",
        passed=False,
        blocks=(
            JudgedBlock("air-tonight", 223.1, -0.468, 0.9965, -1.812, 4, False),
            JudgedBlock("hotel-california", 155.2, -0.681, 0.9991, -2.178, 1, True),
            JudgedBlock("hotel-california", 152.8, last_worst_dbtp, 1.0, -1.715, 0, True),
        ),
    )


def test_a_record_rejudged_under_r17_passes_and_keeps_its_measurements(tmp_path: Path) -> None:
    old = failing_record(make_chain(tmp_path), last_worst_dbtp=-1.111)

    new = clipping_guard.rejudge(old, clipping_guard.R17)

    assert new.passed
    assert new.guard == clipping_guard.R17
    assert all(b.passed for b in new.blocks)
    assert [b.worst_dbtp for b in new.blocks] == [b.worst_dbtp for b in old.blocks]
    assert (new.fingerprint, new.run, new.lead_ins) == (old.fingerprint, old.run, old.lead_ins)


def test_rejudging_refuses_a_record_that_may_leave_out_a_failing_block(tmp_path: Path) -> None:
    # Blocks past the record's last one are only known to be no worse than it. Once that is
    # over the limit, an unrecorded block may fail on its share, so the sweep must run again.
    old = failing_record(make_chain(tmp_path), last_worst_dbtp=-0.95)

    with pytest.raises(ValueError, match="run the guard again"):
        clipping_guard.rejudge(old, clipping_guard.R17)


def test_the_rejudge_command_rewrites_the_presets_record_under_r17(tmp_path: Path) -> None:
    preset = make_chain(tmp_path)
    clipping_guard.write_record(preset, failing_record(preset, last_worst_dbtp=-1.111))

    assert clipping_guard_cli.rejudge_main([str(preset)]) == 0

    record = clipping_guard.read_record(preset)
    assert record is not None and record.passed and record.guard == clipping_guard.R17


def test_the_rejudge_command_refuses_a_record_of_other_files(tmp_path: Path) -> None:
    preset = make_chain(tmp_path)
    clipping_guard.write_record(preset, failing_record(preset, last_worst_dbtp=-1.111))
    preset.write_text(preset.read_text(encoding="utf-8").replace("-2.5", "-2.0"), encoding="utf-8")

    with pytest.raises(SystemExit, match="render it again"):
        clipping_guard_cli.rejudge_main([str(preset)])
