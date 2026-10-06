"""Batch cue analysis: one Liquidsoap process runs autocue over a batch of files.

Spec: Cue pre-computation ("It runs Liquidsoap in batch mode"); D61 (a file that crashes or
hangs the analyser is reported, not the batch); D63 (15 s at startup plus 20 s per file);
D65 (a deadline per file, and every result line validated); D67 (that deadline is
max(20 s, duration / 40)); D68 (a malformed result line fails only its file, the reason
naming the field); D110 (so does a result line that never came before the next file began);
C8 (the child gets only the environment it is given).

Protocol (``cue_analysis.liq``): the child reads its file list from the JSON file named by
``CUE_FILES`` and prints ``RS_CUE_BEGIN <i>`` before and ``RS_CUE_RESULT <json>`` after each
file; every other line is Liquidsoap's log. The protocol is read from stdout alone: FFmpeg
writes its warnings to stderr through a block-buffered C stream, whose flush can end mid-line
and glue the next protocol line onto it, so stderr is drained apart and only kept for
diagnosis. This runs on the cue consumer only, never on an event loop, so it blocks: ``Popen``
and a reader thread per stream.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from backend.playout.errors import AnalyserError
from backend.playout.liquidsoap_process import NO_WINDOW, cache_env, long_path

__all__ = [
    "ANALYSER_SCRIPT",
    "AUTOCUE_SETTINGS",
    "ECHO_CHARS",
    "AnalyserConfig",
    "AnalyserError",
    "BatchAnalysis",
    "CueFile",
    "analyse_batch",
    "echo",
    "remove_listings",
]

ANALYSER_SCRIPT = Path(__file__).with_name("cue_analysis.liq")
AUTOCUE_SETTINGS = Path(__file__).with_name("autocue.liq")  # the one copy; E2's engine too

_BEGIN = "RS_CUE_BEGIN"
_RESULT = "RS_CUE_RESULT"
_TAIL_CHARS = 2_000  # of the output, carried by an AnalyserError
_TAIL_LINES = 200  # enough lines to fill _TAIL_CHARS with Liquidsoap's log
ECHO_CHARS = 200  # of an offending value, quoted in a rejection; shared with autocue.py
_EXIT_WAIT_S = 10.0  # for a child whose output ended to exit by itself, before a kill
_KILL_WAIT_S = 10.0
_DRAIN_WAIT_S = 2.0  # for stderr to end once the child has exited
_AUDIO_S_PER_DEADLINE_S = 40  # D67: autocue runs at about 65x real time; 40x leaves margin
_BACKSLASH_DIGIT = re.compile(r"\\\d")
_LISTING_PREFIX = "cue-files-"  # a batch's file list, in the cache folder


@dataclass(frozen=True)
class CueFile:
    """One file to analyse: its path, and its duration when known (it scales the deadline)."""

    path: str
    duration_ms: int | None


@dataclass(frozen=True)
class AnalyserConfig:
    """How the batch analyser runs; built at the cue consumer's composition root."""

    exe: Path
    cache_dir: Path
    script: Path = ANALYSER_SCRIPT
    startup_timeout_s: float = 15.0  # D63: a cold script cache takes about 5 s
    per_file_timeout_s: float = 20.0  # D63; the floor of D67's per-file deadline

    def __post_init__(self) -> None:
        if _BACKSLASH_DIGIT.search(str(self.exe)):
            raise ValueError(
                "AnalyserConfig.exe must not contain a backslash followed by a digit "
                f"(Liquidsoap 2.4.5 crashes at startup): {self.exe}"
            )
        if not self.startup_timeout_s > 0:  # NaN included
            raise ValueError(
                f"AnalyserConfig.startup_timeout_s must be > 0, got {self.startup_timeout_s}"
            )
        if not self.per_file_timeout_s > 0:  # NaN included
            raise ValueError(
                f"AnalyserConfig.per_file_timeout_s must be > 0, got {self.per_file_timeout_s}"
            )

    def deadline_s(self, duration_ms: int | None) -> float:
        """D67: max(per-file timeout, duration / 40); the per-file timeout when unknown."""
        if duration_ms is None or duration_ms <= 0:
            return self.per_file_timeout_s
        return max(self.per_file_timeout_s, duration_ms / 1000 / _AUDIO_S_PER_DEADLINE_S)


@dataclass(frozen=True)
class BatchAnalysis:
    """What one batch produced, by file index.

    ``metadata``: autocue's metadata for each file whose result line was valid (empty when
    autocue failed on the file). ``rejected``: each file whose result line broke the
    protocol, with the reason naming the field (D68), or never came before the next file
    began (D110). ``stalled``: the file the analyser died or hung on (D61). Files after a
    stalled one were not analysed and appear nowhere.
    """

    metadata: Mapping[int, Mapping[str, str]]
    stalled: int | None
    rejected: Mapping[int, str] = field(default_factory=dict)


type _Line = str | None
"""One line of the child's output; None once the output has ended."""


def _pump(stream: IO[str], lines: queue.Queue[_Line]) -> None:
    """Feed ``stream``'s lines into ``lines`` until it ends, then None (a reader thread)."""
    with stream:
        for line in stream:
            lines.put(line.rstrip("\r\n"))
    lines.put(None)


class _Drain:
    """Keeps the last lines of the child's stderr, read on a thread of its own.

    Reading it at all keeps a chatty decoder from filling the pipe and blocking the child.
    """

    def __init__(self, stream: IO[str]) -> None:
        self._lines: deque[str] = deque(maxlen=_TAIL_LINES)
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._read, args=(stream,), name="cue-analyser-stderr", daemon=True
        )
        self._thread.start()

    def _read(self, stream: IO[str]) -> None:
        with stream:
            for line in stream:
                with self._lock:
                    self._lines.append(line.rstrip("\r\n"))

    def tail(self, wait_s: float) -> str:
        """The last ``_TAIL_CHARS`` characters read, once the stream ended or ``wait_s`` passed."""
        self._thread.join(timeout=wait_s)
        with self._lock:
            return "\n".join(self._lines)[-_TAIL_CHARS:]


def _reap(process: subprocess.Popen[str], exit_wait_s: float) -> int | None:
    """Give ``process`` ``exit_wait_s`` to exit by itself, then kill it and wait for it.

    Its exit code, or None if it had still not exited ``_KILL_WAIT_S`` after the kill.
    """
    with contextlib.suppress(subprocess.TimeoutExpired):
        return process.wait(timeout=exit_wait_s)
    process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        return process.wait(timeout=_KILL_WAIT_S)
    return None


def echo(value: object) -> str:
    """``value``'s repr, truncated to ``ECHO_CHARS`` for a rejection or warning message."""
    return repr(value)[:ECHO_CHARS]


def _result_problem(parsed: object, begun: int, line_no: int) -> str | None:
    """Why a parsed result line breaks the protocol, naming the field; None if it is valid."""
    if not isinstance(parsed, dict):
        return f"{_RESULT} on output line {line_no} is JSON but not an object: {echo(parsed)}"
    index = parsed.get("index")
    if type(index) is not int or index != begun:
        return f"index on output line {line_no} must be {begun}, the file begun: {echo(index)}"
    metadata = parsed.get("metadata")
    if not isinstance(metadata, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in metadata.items()
    ):
        return f"metadata on output line {line_no} must map strings to strings: {echo(metadata)}"
    return None


class _Reader:
    """Follows one child's protocol lines under D65/D67's deadlines, and keeps the outcome."""

    def __init__(self, stream: IO[str], files: Sequence[CueFile], config: AnalyserConfig) -> None:
        self._files = files
        self._config = config
        self._lines: queue.Queue[_Line] = queue.Queue()
        self._tail: deque[str] = deque(maxlen=_TAIL_LINES)
        self._line_no = 0
        self.metadata: dict[int, Mapping[str, str]] = {}
        self.rejected: dict[int, str] = {}
        self.begun: int | None = None
        self.ended = False  # the child's output ended (it exited, or is exiting)
        pump = threading.Thread(
            target=_pump, args=(stream, self._lines), name="cue-analyser-output", daemon=True
        )
        pump.start()

    def tail(self) -> str:
        """The last ``_TAIL_CHARS`` characters of the output read so far."""
        return "\n".join(self._tail)[-_TAIL_CHARS:]

    def settled(self, index: int) -> bool:
        return index in self.metadata or index in self.rejected

    def run(self) -> None:
        """Read until every file has settled, the output ends, or a deadline passes.

        The first protocol line must come within the startup timeout; after a begin, the
        next within that file's deadline (D67); after a result, within the per-file timeout.
        Raises ``AnalyserError`` if a begin is out of order or a result has no file.
        """
        allowed = self._config.startup_timeout_s
        while len(self.metadata) + len(self.rejected) < len(self._files):
            line = self._next_protocol_line(time.monotonic() + allowed)
            if line is None:
                return
            head, _, body = line.partition(" ")
            if head == _BEGIN:
                expected = self._next_index()
                self._begin(body, expected)
                allowed = self._config.deadline_s(self._files[expected].duration_ms)
            else:
                self._result(body)
                allowed = self._config.per_file_timeout_s

    def _next_protocol_line(self, deadline: float) -> str | None:
        """The next protocol line; None when the output ends or ``deadline`` passes.

        Log lines are kept for the tail and do not move the deadline.
        """
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                self.ended = True
                return None
            self._line_no += 1
            self._tail.append(line)
            if line.partition(" ")[0] in (_BEGIN, _RESULT):
                return line
        return None

    def _next_index(self) -> int:
        """The index the next ``RS_CUE_BEGIN`` must carry."""
        return 0 if self.begun is None else self.begun + 1

    def _begin(self, body: str, expected: int) -> None:
        """Record that file ``expected`` has begun.

        A begin that skips a file, repeats one, or goes beyond the batch breaks the protocol.
        D110: the next file beginning before the previous file's result rejects that file, as
        a malformed result line does (D68), and the batch goes on.
        """
        if body != str(expected) or expected >= len(self._files):
            raise AnalyserError(
                f"{_BEGIN} on output line {self._line_no} must be {expected} "
                f"of {len(self._files)} files: {body!r}"
            )
        if self.begun is not None and not self.settled(self.begun):
            self.rejected[self.begun] = (
                f"no {_RESULT} for file {self.begun} before {_BEGIN} {expected} "
                f"on output line {self._line_no}"
            )
        self.begun = expected

    def _result(self, body: str) -> None:
        begun = self.begun
        if begun is None or self.settled(begun):
            raise AnalyserError(
                f"{_RESULT} on output line {self._line_no} with no file awaiting a result"
            )
        try:
            parsed: object = json.loads(body)
        except json.JSONDecodeError:
            self.rejected[begun] = (  # D68: this file fails; the batch goes on
                f"{_RESULT} on output line {self._line_no} is not JSON: {echo(body)}"
            )
            return
        problem = _result_problem(parsed, begun, self._line_no)
        if problem is not None:
            self.rejected[begun] = problem
            return
        assert isinstance(parsed, dict)  # _result_problem checked it
        self.metadata[begun] = dict(parsed["metadata"])


def _write_listing(files: Sequence[CueFile], cache_dir: Path) -> Path:
    """The files' long paths, as a JSON list in a new file in ``cache_dir``."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=_LISTING_PREFIX, suffix=".json", dir=cache_dir)
    with os.fdopen(handle, "w", encoding="utf-8") as listing:
        json.dump(
            [long_path(Path(cue_file.path)) for cue_file in files],
            listing,
            ensure_ascii=False,
        )
    return Path(name)


def remove_listings(cache_dir: Path) -> None:
    """Delete the file listings in ``cache_dir``; call only while no batch is running.

    ``analyse_batch`` deletes its own listing, unless its process is killed hard mid-batch.
    """
    for listing in cache_dir.glob(f"{_LISTING_PREFIX}*.json"):
        listing.unlink(missing_ok=True)


def _start(
    listing: Path, base_env: Mapping[str, str], config: AnalyserConfig
) -> subprocess.Popen[str]:
    try:
        return subprocess.Popen(
            [str(config.exe), str(config.script)],
            env={**base_env, **cache_env(config.cache_dir), "CUE_FILES": str(listing)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # not the protocol: see the module docstring
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW,
        )
    except OSError as refused:
        raise AnalyserError(f"cue analyser did not start: {refused}") from refused


def analyse_batch(
    files: Sequence[CueFile], base_env: Mapping[str, str], config: AnalyserConfig
) -> BatchAnalysis:
    """Run autocue on ``files`` in one process; results are matched to files by index.

    A file the process died or hung on is ``stalled``, and later files are not analysed. A
    file whose result line is malformed, or missing when the next file begins, is ``rejected``,
    and the rest go on (D68, D110). Raises ``AnalyserError`` when the process did not start
    or never began a file (a broken install or script), or broke the protocol in any other
    way: a begin out of order or beyond the batch, or a result with no file awaiting it. The
    process never outlives the call.
    """
    if not files:
        return BatchAnalysis(metadata={}, stalled=None)
    listing = _write_listing(files, config.cache_dir)
    try:
        process = _start(listing, base_env, config)
        reader: _Reader | None = None
        try:
            assert process.stdout is not None and process.stderr is not None  # both PIPE
            errors = _Drain(process.stderr)
            reader = _Reader(process.stdout, files, config)  # starts the pump thread
            reader.run()
        finally:
            output_ended = reader is not None and reader.ended
            code = _reap(process, _EXIT_WAIT_S if output_ended else 0.0)
    finally:
        listing.unlink(missing_ok=True)
    if reader.begun is None:
        never_began = AnalyserError(f"cue analyser exit code {code}: {reader.tail()}")
        if not reader.ended:
            never_began.add_note(
                f"killed: no {_BEGIN} within the {config.startup_timeout_s} s startup timeout"
            )
        if stderr := errors.tail(_DRAIN_WAIT_S):
            never_began.add_note(f"stderr: {stderr}")
        raise never_began
    stalled = None if reader.settled(reader.begun) else reader.begun
    return BatchAnalysis(metadata=reader.metadata, stalled=stalled, rejected=reader.rejected)
