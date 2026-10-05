"""D110: a missing result line fails only its file (user, 2026-10-05; refines D65 and D68).

If the analyser begins file i + 1 (the next index, as expected) before printing a result line
for file i, file i is rejected as D68 does it, its reason naming the file and the output line,
and the batch goes on. Every other protocol break still fails the batch: an index that is
skipped, repeated or beyond the batch, or a result line with no file awaiting it. A file whose
result is missing when the output ends is still stalled (D61).

A Python script stands in for Liquidsoap, as in test_cue_analysis.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from backend.playout.cue_analysis import AnalyserConfig, BatchAnalysis, CueFile, analyse_batch
from backend.playout.errors import AnalyserError
from backend.playout.liquidsoap_process import session_base_env

PYTHON = Path(getattr(sys, "_base_executable", sys.executable))

HEAD = r"""
import json, os, pathlib, sys
paths = json.loads(pathlib.Path(os.environ["CUE_FILES"]).read_text(encoding="utf-8"))
print("2026/10/05 12:09:31 [main:3] Liquidsoap log noise", flush=True)

def begin(i):
    print(f"RS_CUE_BEGIN {i}", flush=True)

def result(i):
    line = {"index": i, "metadata": {"liq_cue_in": str(i)}}
    print("RS_CUE_RESULT " + json.dumps(line), flush=True)
"""
NO_RESULT_FOR_FIRST = r"""
begin(0)
begin(1)
result(1)
"""
NO_RESULT_FOR_FIRST_AND_THIRD = r"""
begin(0)
begin(1)
result(1)
begin(2)
begin(3)
result(3)
"""
NO_RESULT_THEN_DIES = r"""
begin(0)
begin(1)
sys.exit(3)
"""
# Protocol breaks D110 leaves alone: the file count, and the script's protocol lines. A begin
# beyond the batch is only reachable while the last file has no result: once every file has
# its outcome, the reader stops and never reads the begin.
STILL_BREAKS_THE_BATCH = {
    "skipped index": (3, "begin(0)\nresult(0)\nbegin(2)\n"),
    "repeated index": (3, "begin(0)\nresult(0)\nbegin(0)\n"),
    "skipped index after a missing result": (3, "begin(0)\nbegin(2)\nresult(2)\n"),
    "repeated index without a result": (3, "begin(0)\nbegin(0)\nresult(0)\n"),
    "begin beyond the batch after the last file's missing result": (
        1,
        "begin(0)\nbegin(1)\nresult(1)\n",
    ),
    "result with no file awaiting it": (3, "begin(0)\nresult(0)\nresult(0)\n"),
    "result before any begin": (1, "result(0)\n"),
}


def run(tmp_path: Path, body: str, count: int) -> BatchAnalysis:
    script = tmp_path / "fake_analyser.py"
    script.write_text(HEAD + body, encoding="utf-8")
    config = AnalyserConfig(
        exe=PYTHON,
        cache_dir=tmp_path / "cache",
        script=script,
        startup_timeout_s=10.0,
        per_file_timeout_s=10.0,
    )
    files = [CueFile(path=f"D:/{i}.flac", duration_ms=None) for i in range(count)]
    return analyse_batch(files, session_base_env(os.environ), config)


def assert_names_missing_result(reason: str, index: int) -> None:
    """D110: the reason names the file and the output line, in a form a log search finds."""
    assert reason.startswith(f"no RS_CUE_RESULT for file {index}"), reason
    assert "output line" in reason, reason


def test_a_begin_before_a_result_fails_only_the_earlier_file(tmp_path: Path) -> None:
    """File 0 has no result when file 1 begins: file 0 is rejected, file 1 is analysed. The
    analyser logs nothing itself: the warning downstream, once per file, is the only signal."""
    with capture_logs() as logs:
        batch = run(tmp_path, NO_RESULT_FOR_FIRST, 2)
    assert logs == []
    assert (sorted(batch.rejected), sorted(batch.metadata), batch.stalled) == ([0], [1], None)
    assert_names_missing_result(batch.rejected[0], 0)
    assert batch.metadata[1]["liq_cue_in"] == "1"


def test_each_missing_result_fails_only_its_own_file(tmp_path: Path) -> None:
    """Two gaps in one batch: each is rejected on its own, and the batch goes on past both."""
    batch = run(tmp_path, NO_RESULT_FOR_FIRST_AND_THIRD, 4)
    assert (sorted(batch.rejected), sorted(batch.metadata), batch.stalled) == ([0, 2], [1, 3], None)
    assert_names_missing_result(batch.rejected[0], 0)
    assert_names_missing_result(batch.rejected[2], 2)


def test_a_missing_result_when_the_output_ends_is_still_a_stall(tmp_path: Path) -> None:
    """D61 unchanged: the file the analyser died on is stalled, not rejected; the file before
    it, begun over without a result, is rejected (D110)."""
    batch = run(tmp_path, NO_RESULT_THEN_DIES, 3)
    assert (sorted(batch.rejected), dict(batch.metadata), batch.stalled) == ([0], {}, 1)
    assert_names_missing_result(batch.rejected[0], 0)


@pytest.mark.parametrize(
    ("count", "body"), STILL_BREAKS_THE_BATCH.values(), ids=list(STILL_BREAKS_THE_BATCH)
)
def test_every_other_protocol_break_still_fails_the_batch(
    tmp_path: Path, count: int, body: str
) -> None:
    """D110 applies only when the begin carries the next index; anything else is an error."""
    with pytest.raises(AnalyserError):
        run(tmp_path, body, count)
