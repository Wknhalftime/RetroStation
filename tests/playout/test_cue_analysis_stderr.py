"""The batch analyser's stderr is not part of the protocol.

FFmpeg, inside Liquidsoap, writes its warnings to stderr through a block-buffered C stream
(4096 bytes on Windows), so a flush can end mid-line. On 2026-10-05 such a flush ended in
"[flac @ 000001e018c529c0" and the next protocol line, "RS_CUE_RESULT ...", was glued on
after it; the reader never saw that result and failed the whole batch at the next
"RS_CUE_BEGIN". The protocol is read from stdout alone; stderr is drained apart and kept
for diagnosis. A Python script stands in for Liquidsoap, as in test_cue_analysis.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from backend.playout.cue_analysis import AnalyserConfig, BatchAnalysis, CueFile, analyse_batch
from backend.playout.errors import AnalyserError
from backend.playout.liquidsoap_process import session_base_env

PYTHON = Path(getattr(sys, "_base_executable", sys.executable))

HEAD = r"""
import json, os, pathlib, sys
paths = json.loads(pathlib.Path(os.environ["CUE_FILES"]).read_text(encoding="utf-8"))

def begin(i):
    print(f"RS_CUE_BEGIN {i}", flush=True)

def result(i):
    line = {"index": i, "metadata": {"liq_cue_in": str(i)}}
    print("RS_CUE_RESULT " + json.dumps(line), flush=True)

def stderr(text):
    sys.stderr.write(text)
    sys.stderr.flush()
"""
FRAGMENT_BEFORE_RESULT = r"""
begin(0)
stderr("[flac @ 000001e018c529c0")
result(0)
begin(1)
stderr("] Discarding ID3 tags because more suitable tags were found.\n")
result(1)
"""
"""The 2026-10-05 output: a buffered stderr flush that ends mid-line, just before a result."""
FLOODS_STDERR = r"""
for i in range(len(paths)):
    begin(i)
    stderr("[flac @ 000001e018c529c0] invalid frame header\n" * 6_000)
    result(i)
"""
"""About 290 kB of stderr per file: more than a pipe holds, so stderr must be read."""
NEVER_BEGINS_STDERR = r"""
stderr("fatal: the decoder aborted\n")
sys.exit(2)
"""


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


def test_a_stderr_fragment_does_not_split_a_protocol_line(tmp_path: Path) -> None:
    """Each file's result is read although stderr ended mid-line just before it."""
    batch = run(tmp_path, FRAGMENT_BEFORE_RESULT, 2)
    assert (batch.stalled, dict(batch.rejected)) == (None, {})
    assert sorted(batch.metadata) == [0, 1]


def test_stderr_beyond_a_pipe_buffer_does_not_stall_the_analyser(tmp_path: Path) -> None:
    """stderr is drained while the batch runs, so a chatty decoder never blocks the child."""
    batch = run(tmp_path, FLOODS_STDERR, 2)
    assert (batch.stalled, sorted(batch.metadata)) == (None, [0, 1])


def test_an_analyser_that_never_begins_keeps_its_stderr(tmp_path: Path) -> None:
    """A child that fails before any file still says why, when it said so on stderr."""
    with pytest.raises(AnalyserError) as raised:
        run(tmp_path, NEVER_BEGINS_STDERR, 1)
    told = "\n".join([str(raised.value), *getattr(raised.value, "__notes__", [])])
    assert "fatal: the decoder aborted" in told
