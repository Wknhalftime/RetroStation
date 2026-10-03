"""The batch analyser process (spec: Cue pre-computation "It runs Liquidsoap in batch mode";
D63 timeouts 15 s at startup plus 20 s per file; D65 "a deadline per file, not one timeout for
the whole batch"; D67 the per-file deadline is max(20 s, duration / 40), 20 s with no
duration; D68 "a malformed result line fails only that file", the error naming the field;
Engine: paths with the \\\\?\\ prefix; C8: the engine gets no secrets). A Python script stands
in for Liquidsoap, so these run on CI; the one path-syntax test is Windows-only.

Protocol (Task 5): the child reads its file list from the JSON file named by CUE_FILES and
prints "RS_CUE_BEGIN <i>" before and "RS_CUE_RESULT <json>" after each file; other lines
are Liquidsoap's log and are ignored.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from backend.playout.cue_analysis import AnalyserConfig, CueFile, analyse_batch
from backend.playout.errors import AnalyserError
from backend.playout.liquidsoap_process import session_base_env

PYTHON = Path(getattr(sys, "_base_executable", sys.executable))
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows path syntax")

HEAD = r"""
import json, os, pathlib, sys, time
paths = json.loads(pathlib.Path(os.environ["CUE_FILES"]).read_text(encoding="utf-8"))
here = pathlib.Path(sys.argv[0])
here.with_suffix(".env.json").write_text(json.dumps(dict(os.environ)), encoding="utf-8")
here.with_suffix(".paths.json").write_text(json.dumps(paths), encoding="utf-8")
here.with_suffix(".pid").write_text(str(os.getpid()), encoding="utf-8")
print("2026/09/30 12:00:00 [main:3] Liquidsoap log noise", flush=True)

def begin(i):
    print(f"RS_CUE_BEGIN {i}", flush=True)

def result(i):
    line = {"index": i, "metadata": {"liq_cue_in": str(i)}}
    print("RS_CUE_RESULT " + json.dumps(line), flush=True)
"""
ALL_OK = r"""
for i in range(len(paths)):
    begin(i)
    result(i)
"""
DIES_ON_SECOND = r"""
begin(0)
result(0)
begin(1)
sys.exit(3)
"""
HANGS = r"""
begin(0)
time.sleep(60)
"""
SLOW_FIRST_FILE = r"""
begin(0)
time.sleep(2.5)
result(0)
begin(1)
result(1)
"""
COLD_START = r"""
time.sleep(1.2)
begin(0)
result(0)
"""
NEVER_BEGINS = r"""
print("error: the script failed to compile", flush=True)
sys.exit(2)
"""
INVALID_RESULTS = {
    "no metadata": ("metadata", "print('RS_CUE_RESULT {\"index\": 0}')"),
    "metadata not strings": (
        "metadata",
        'print(\'RS_CUE_RESULT {"index": 0, "metadata": {"a": 0.4}}\')',
    ),
    "no index": ("index", "print('RS_CUE_RESULT {\"metadata\": {}}')"),
    "not JSON": ("JSON", "print('RS_CUE_RESULT not a result')"),
}
"""A malformed result line for file 0: what it breaks, and the line; file 1 is then analysed.
The lines leave out or misuse the field they break, so a reason that only echoed the line
would name the wrong field, or none."""
FIELDS = {"metadata", "index", "JSON"}


def env() -> dict[str, str]:
    return session_base_env(os.environ)


def files(*paths: str) -> list[CueFile]:
    """Files of unknown duration: each gets the plain per-file deadline (D67)."""
    return [CueFile(path=path, duration_ms=None) for path in paths]


def config(tmp_path: Path, body: str, **timeouts: float) -> AnalyserConfig:
    script = tmp_path / "fake_analyser.py"
    script.write_text(HEAD + body, encoding="utf-8")
    return AnalyserConfig(exe=PYTHON, cache_dir=tmp_path / "cache", script=script, **timeouts)


def pid_of_fake(tmp_path: Path) -> int:
    return int((tmp_path / "fake_analyser.pid").read_text(encoding="utf-8"))


def test_each_files_metadata_is_returned_by_its_index(tmp_path: Path) -> None:
    """Batch mode: one process analyses a batch; results are matched to files by index."""
    batch = analyse_batch(files("D:/a.flac", "D:/b.flac"), env(), config(tmp_path, ALL_OK))
    assert (batch.stalled, dict(batch.rejected)) == (None, {})
    assert sorted(batch.metadata) == [0, 1]
    assert batch.metadata[1]["liq_cue_in"] == "1"


def test_a_file_the_analyser_dies_on_is_reported_stalled(tmp_path: Path) -> None:
    """D61: the file being analysed when the process died is named; later files are neither
    analysed nor blamed."""
    batch = analyse_batch(files("a", "b", "c"), env(), config(tmp_path, DIES_ON_SECOND))
    assert (sorted(batch.metadata), batch.stalled) == ([0], 1)


def test_a_hung_analyser_is_killed_and_its_file_reported_stalled(tmp_path: Path) -> None:
    """D61, D65: a hang is cut at its file's deadline, and the process does not outlive it."""
    began = time.monotonic()
    batch = analyse_batch(
        files("a"), env(), config(tmp_path, HANGS, startup_timeout_s=5.0, per_file_timeout_s=1.0)
    )
    assert (dict(batch.metadata), batch.stalled) == ({}, 0)
    assert time.monotonic() - began < 10.0
    with contextlib.suppress(psutil.NoSuchProcess):
        psutil.Process(pid_of_fake(tmp_path)).wait(timeout=5)


def test_a_slow_file_is_blamed_not_the_next_one(tmp_path: Path) -> None:
    """D65: file 0 is cut at its own 1 s deadline. One timeout for the whole batch
    (5 + 2 x 1 s = 7 s) would let its 2.5 s pass, and so would timing every line by the 5 s
    startup deadline; a batch clock that ran out later would blame the healthy file 1."""
    batch = analyse_batch(
        files("a", "b"),
        env(),
        config(tmp_path, SLOW_FIRST_FILE, startup_timeout_s=5.0, per_file_timeout_s=1.0),
    )
    assert (dict(batch.metadata), batch.stalled) == ({}, 0)


def test_a_long_file_gets_a_deadline_scaled_to_its_length(tmp_path: Path) -> None:
    """D67: max(per-file deadline, duration / 40). A 200 s file gets 5 s here, so file 0's
    2.5 s is not a stall; a 10 s file keeps the plain 1 s deadline and is cut."""
    long_first = [CueFile("a", duration_ms=200_000), CueFile("b", duration_ms=10_000)]
    batch = analyse_batch(
        long_first,
        env(),
        config(tmp_path, SLOW_FIRST_FILE, startup_timeout_s=5.0, per_file_timeout_s=1.0),
    )
    assert (sorted(batch.metadata), batch.stalled) == ([0, 1], None)
    short_first = [CueFile("a", duration_ms=10_000), CueFile("b", duration_ms=200_000)]
    batch = analyse_batch(
        short_first,
        env(),
        config(tmp_path, SLOW_FIRST_FILE, startup_timeout_s=5.0, per_file_timeout_s=1.0),
    )
    assert (dict(batch.metadata), batch.stalled) == ({}, 0)


@pytest.mark.parametrize(
    ("duration_ms", "deadline_s"),
    [(None, 20.0), (0, 20.0), (180_000, 20.0), (800_000, 20.0), (1_020_000, 25.5)],
    ids=["unknown", "zero", "3 min", "800 s", "17 min"],
)
def test_the_per_file_deadline_is_the_ruling(
    tmp_path: Path, duration_ms: int | None, deadline_s: float
) -> None:
    """D67: max(20 s, duration / 40); 20 s with no duration ("a 17-minute song gets about
    26 s")."""
    analyser = AnalyserConfig(exe=PYTHON, cache_dir=tmp_path)
    assert analyser.deadline_s(duration_ms) == pytest.approx(deadline_s)


def test_a_cold_start_is_timed_by_the_startup_deadline(tmp_path: Path) -> None:
    """D63/D65: the first line waits for the startup deadline (a cold script cache takes
    about 5 s), not the per-file one."""
    batch = analyse_batch(
        files("a"),
        env(),
        config(tmp_path, COLD_START, startup_timeout_s=3.0, per_file_timeout_s=0.5),
    )
    assert (sorted(batch.metadata), batch.stalled) == ([0], None)


def test_an_analyser_that_never_begins_is_an_error_with_its_output(tmp_path: Path) -> None:
    """Never skip error handling: a broken install or script is an error carrying the exit
    code and what the process printed, not a failed analysis of the first file."""
    with pytest.raises(AnalyserError, match="exit code 2") as raised:
        analyse_batch(files("a"), env(), config(tmp_path, NEVER_BEGINS))
    assert "failed to compile" in str(raised.value)


@pytest.mark.parametrize(("field", "line"), INVALID_RESULTS.values(), ids=INVALID_RESULTS.keys())
def test_a_malformed_result_line_fails_only_its_file(tmp_path: Path, field: str, line: str) -> None:
    """D68: the file whose result line breaks the protocol is rejected, with the field named
    (metadata must map strings to strings, the index must be the file just begun, the line
    must be JSON), and no other; the rest of the batch is analysed."""
    body = f"begin(0)\n{line}\nbegin(1)\nresult(1)\n"
    batch = analyse_batch(files("a", "b"), env(), config(tmp_path, body))
    assert (sorted(batch.metadata), batch.stalled, sorted(batch.rejected)) == ([1], None, [0])
    reason = batch.rejected[0]
    assert field in reason
    assert not [other for other in FIELDS - {field} if other in reason]


def test_no_files_starts_no_analyser(tmp_path: Path) -> None:
    """An empty batch costs nothing."""
    batch = analyse_batch([], env(), config(tmp_path, ALL_OK))
    assert (dict(batch.metadata), batch.stalled) == ({}, None)
    assert not (tmp_path / "fake_analyser.pid").exists()


def test_the_analyser_gets_the_given_environment_and_its_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C8: the child gets only what it is given plus its cache variables, never the API's
    secrets; the script cache lives in the analyser's own folder."""
    monkeypatch.setenv("AIRWAVE_TOKEN", "secret")
    analyse_batch(files("a"), env(), config(tmp_path, ALL_OK))
    seen = json.loads((tmp_path / "fake_analyser.env.json").read_text(encoding="utf-8"))
    assert "AIRWAVE_TOKEN" not in seen
    assert seen["LIQ_CACHE_DIR"] == str((tmp_path / "cache").resolve())


@WINDOWS_ONLY
def test_paths_are_sent_in_long_path_form(tmp_path: Path) -> None:
    """Engine: "Paths are sent with the \\\\?\\ prefix"; a share takes \\\\?\\UNC\\."""
    sent_files = files(r"D:\Music\a.flac", r"\\nas\music\b.flac")
    analyse_batch(sent_files, env(), config(tmp_path, ALL_OK))
    sent = json.loads((tmp_path / "fake_analyser.paths.json").read_text(encoding="utf-8"))
    assert sent == [r"\\?\D:\Music\a.flac", r"\\?\UNC\nas\music\b.flac"]


@pytest.mark.parametrize("field", ["startup_timeout_s", "per_file_timeout_s"])
def test_the_timeouts_must_be_positive(tmp_path: Path, field: str) -> None:
    """House rule: validate on load and name the field."""
    with pytest.raises(ValueError, match=f"AnalyserConfig.{field}"):
        AnalyserConfig(exe=PYTHON, cache_dir=tmp_path, **{field: 0.0})


def test_the_timeouts_default_to_the_rulings(tmp_path: Path) -> None:
    """D63: 15 s at startup plus 20 s per file."""
    analyser = AnalyserConfig(exe=PYTHON, cache_dir=tmp_path)
    assert (analyser.startup_timeout_s, analyser.per_file_timeout_s) == (15.0, 20.0)
