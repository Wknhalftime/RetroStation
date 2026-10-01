"""Cue pre-computation: one bounded run over the audio that needs analysis.

Spec: Cue pre-computation; D20 (needs analysis; an analyser change purges its old rows);
D52/D55 (the fallback row; no duration, retried); D57 (an unreadable or changed file stores
nothing); D59 (no progress rows: one summary log); D61 (the first file with no outcome is
the stall and gets a fallback row; the files after it go back, untried); D62 (today's and
tomorrow's playlists first); D63 (batches of 8, a bounded run); D64 (an unknown stored stat
trusts the hash); D67 (each file's duration reaches the analyser); D68 (a rejected result
line fails only its file).

The pipeline, one function per step:

1. purge the other analyser versions, and commit;
2. the priority set, read once and batched in memory; then the library, keyset by path;
3. per batch: drop what this run has seen, check the stat, analyse, check the stat again,
   record, commit;
4. stop when both sources are exhausted, or after a batch once the time budget is spent;
5. log one summary, unless the run tried nothing.

D79: the player reports a no-cue song to the cue owner, which analyses that audio ahead of
its queue, storing cues or a failed row as a run does. ``analyse_reported`` is a report: one
file, through the same batch step (``_run_batch``) a run uses. No purge, no priority set, no
library read; writes nothing else.

``Analyse`` blocks for as long as a batch takes, so a run belongs on the cue worker, never on
an event loop. An ``AnalyserError`` ends the run and reaches the task boundary; the batches
before it are committed.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from uuid import UUID

import structlog

from backend.domain.library import AudioHash
from backend.domain.streaming import CUE_ANALYSER_VERSION, CueCandidate, StreamTiming
from backend.domain.tune_in import tune_in_days
from backend.playout.cue_analysis import BatchAnalysis, CueFile
from backend.repositories.stream_cue_work import CueWorkRepository
from backend.repositories.stream_cues import StreamCueRepository
from backend.services.audio_tags import disk_stat
from backend.services.streaming.autocue import Unusable
from backend.services.streaming.cue_record import Autocued, CueRecord, record_analysis

logger = structlog.get_logger()

type Analyse = Callable[[Sequence[CueFile]], BatchAnalysis]
"""playout's ``analyse_batch`` with its environment and configuration bound (D67: each file
goes with its duration)."""

_UNREADABLE = "unreadable"
"""D57: a file that cannot be stat'ed stores nothing and is retried next run."""

_NONE_NEEDED = "none_needed"
"""D79: the reported file's audio already has a row, or there is nothing to report on."""

_STORED = frozenset({CueRecord.ANALYSED, CueRecord.FAILED})
_NOTHING_READY = BatchAnalysis(metadata={}, stalled=None)
_STALLED = Unusable("analyser stalled or died on this file (D61)")


@dataclass(frozen=True)
class CueRunPorts:
    """What a run reads, writes and runs."""

    work: CueWorkRepository
    store: StreamCueRepository
    commit: Callable[[], None]
    analyse: Analyse


@dataclass(frozen=True)
class CueRunConfig:
    """When a run is, how it is paced, and the fallback row's fades."""

    today: date
    steady: Callable[[], float]
    batch_size: int = 8  # D63
    budget_s: float = 240.0  # N2: a run ends inside the 5-minute resume cadence
    timing: StreamTiming = field(default_factory=StreamTiming)

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError(f"CueRunConfig.batch_size must be >= 1, got {self.batch_size}")
        if not self.budget_s > 0:  # NaN included
            raise ValueError(f"CueRunConfig.budget_s must be > 0, got {self.budget_s}")


class _Backlog:
    """The run's work in order: the priority set, batched in memory, then the library in
    keyset batches by path."""

    def __init__(
        self, work: CueWorkRepository, priority: Sequence[CueCandidate], size: int
    ) -> None:
        self._work = work
        self._priority = deque(priority)
        self._size = size
        self._cursor: str | None = None
        self._library_done = False
        self._last_from_library = False

    def take(self) -> list[CueCandidate]:
        """The next batch; empty once both sources are exhausted."""
        if self._priority:
            self._last_from_library = False
            count = min(self._size, len(self._priority))
            return [self._priority.popleft() for _ in range(count)]
        if self._library_done:
            return []
        rows = self._work.library(self._cursor, self._size)
        self._last_from_library = True
        if not rows:
            self._library_done = True
            return []
        self._cursor = rows[-1].path
        return rows

    def put_back(self, stalled: CueCandidate, untried: Sequence[CueCandidate]) -> None:
        """The files after a stall in the last batch: back at the front of the priority set,
        or, for a library batch, the cursor goes back to the stalled file so the next read
        returns them."""
        if self._last_from_library:
            self._cursor = stalled.path
        else:
            self._priority.extendleft(reversed(untried))


@dataclass
class _Run:
    """One run's state: its backlog, the files it tried, the audio it stored, its counts."""

    backlog: _Backlog
    tried: set[UUID] = field(default_factory=set)
    recorded: set[AudioHash] = field(default_factory=set)
    counts: Counter[str] = field(default_factory=Counter)

    def count(self, outcomes: Iterable[str]) -> None:
        self.counts.update(outcomes)


def _purge_other_versions(ports: CueRunPorts) -> None:
    """D20: an analyser change purges its old rows, before anything is analysed."""
    ports.store.purge_other_versions(CUE_ANALYSER_VERSION)
    ports.commit()


def _priority_set(work: CueWorkRepository, today: date) -> list[CueCandidate]:
    """D62: the files playing today and tomorrow at every station, in every logged year."""
    return work.scheduled(tune_in_days(today, work.logged_years()))


def _unseen(batch: Sequence[CueCandidate], run: _Run) -> list[CueCandidate]:
    """``batch`` without the files this run has tried and the audio it has already stored.

    Twins (same audio) are not deduplicated here: M1, each one still needs its own stat
    pre-check, so an unreadable or changed twin cannot hide a readable one. See
    ``_dedupe_audio``, which runs after that check.
    """
    return [
        candidate
        for candidate in batch
        if candidate.file_id not in run.tried and candidate.audio_hash not in run.recorded
    ]


def _dedupe_audio(candidates: Sequence[CueCandidate]) -> list[CueCandidate]:
    """``candidates`` keeping only the first file for each audio hash: twins share one cue
    row, so only one needs analysing. Call only with files that passed the stat pre-check
    (M1), so a problem on one twin cannot hide a readable one in the same batch."""
    seen: set[AudioHash] = set()
    unique: list[CueCandidate] = []
    for candidate in candidates:
        if candidate.audio_hash in seen:
            continue
        seen.add(candidate.audio_hash)
        unique.append(candidate)
    return unique


def _stat_problem(candidate: CueCandidate) -> str | None:
    """Why the file on disk may not be the file hashed (D57), or None when it is. A NULL
    stored size or mtime skips the comparison (D64)."""
    try:
        stat = disk_stat(Path(candidate.path))
    except OSError as error:
        logger.debug("stream_cue_file_unreadable", path=candidate.path, error=str(error))
        return _UNREADABLE
    if candidate.file_size is None or candidate.file_mtime_ns is None:
        return None
    if (stat.size, stat.mtime_ns) != (candidate.file_size, candidate.file_mtime_ns):
        logger.debug("stream_cue_file_changed", path=candidate.path)
        return CueRecord.CHANGED
    return None


def _analyse(analyse: Analyse, ready: Sequence[CueCandidate]) -> BatchAnalysis:
    """Autocue over the ready files, each with its duration (D67); none started for none."""
    if not ready:
        return _NOTHING_READY
    return analyse([CueFile(c.path, c.duration_ms) for c in ready])


def _stalled_at(count: int, analysis: BatchAnalysis) -> int | None:
    """The first of ``count`` files with no outcome: the file the analyser stalled or died
    on (D61), whether or not it reported the stall; every later file is untried. None when
    every file has an outcome."""
    for index in range(count):
        if index == analysis.stalled:
            return index
        if index not in analysis.metadata and index not in analysis.rejected:
            return index
    return None


def _autocued(analysis: BatchAnalysis, index: int, candidate: CueCandidate) -> Autocued:
    """What autocue said about one file with an outcome: a rejected line's reason (D68), or
    its metadata."""
    if index in analysis.rejected:
        return Autocued(candidate, Unusable(analysis.rejected[index]))
    return Autocued(candidate, analysis.metadata[index])


def _record_one(store: StreamCueRepository, timing: StreamTiming, autocued: Autocued) -> str:
    """Store one analysed file's cues if its stat still matches; the post-check's problem,
    or what recording the analysis did."""
    problem = _stat_problem(autocued.candidate)
    if problem is not None:
        return problem
    return record_analysis(store, timing, autocued)


def _record_batch(
    store: StreamCueRepository, timing: StreamTiming, run: _Run, analysed: Sequence[Autocued]
) -> None:
    """Post-check each analysed file's stat, then store what is still current."""
    for autocued in analysed:
        outcome = _record_one(store, timing, autocued)
        run.count([outcome])
        if outcome in _STORED:
            run.recorded.add(autocued.candidate.audio_hash)


def _run_batch(
    ports: CueRunPorts, timing: StreamTiming, run: _Run, batch: Sequence[CueCandidate]
) -> None:
    """Check, analyse, check again, record and commit one batch."""
    unseen = _unseen(batch, run)
    run.tried.update(c.file_id for c in unseen)
    problems = {c.file_id: p for c in unseen if (p := _stat_problem(c)) is not None}
    run.count(problems.values())
    passed = [c for c in unseen if c.file_id not in problems]
    ready = _dedupe_audio(passed)
    ports.commit()  # M2: no transaction sits idle during analysis
    analysis = _analyse(ports.analyse, ready)
    stalled = _stalled_at(len(ready), analysis)
    settled = ready if stalled is None else ready[:stalled]
    analysed = [_autocued(analysis, i, c) for i, c in enumerate(settled)]
    if stalled is not None:
        analysed.append(Autocued(ready[stalled], _STALLED))
        untried = ready[stalled + 1 :]
        run.backlog.put_back(ready[stalled], untried)
        run.tried.difference_update(c.file_id for c in untried)
    _record_batch(ports.store, timing, run, analysed)
    ports.commit()


def _log_summary(run: _Run, paused: bool) -> None:
    """D59: the run's counts as one event; an idle run leaves no trace. I2: a run that stores
    nothing and is not paused logs at debug, not info, so a stuck unreadable or changed file
    does not spam System Logs every cadence."""
    if not run.tried:
        return
    stored = run.counts[CueRecord.ANALYSED] + run.counts[CueRecord.FAILED]
    log = logger.info if stored > 0 or paused else logger.debug
    log(
        "stream_cue_run",
        analysed=run.counts[CueRecord.ANALYSED],
        failed=run.counts[CueRecord.FAILED],
        no_duration=run.counts[CueRecord.NO_DURATION],
        changed=run.counts[CueRecord.CHANGED],
        unreadable=run.counts[_UNREADABLE],
        paused=paused,
    )


def run_cue_analysis(ports: CueRunPorts, config: CueRunConfig) -> None:
    """One bounded run. Its results are the store's state and one ``stream_cue_run`` log.

    An ``AnalyserError`` propagates to the task boundary; the batches before it are
    committed.
    """
    started = config.steady()
    _purge_other_versions(ports)
    run = _Run(_Backlog(ports.work, _priority_set(ports.work, config.today), config.batch_size))
    paused = False
    while batch := run.backlog.take():
        _run_batch(ports, config.timing, run, batch)
        if config.steady() - started >= config.budget_s:
            paused = True
            break
    _log_summary(run, paused)


def analyse_reported(ports: CueRunPorts, timing: StreamTiming, file_id: UUID) -> None:
    """D79: analyse one reported file's audio ahead of the backlog, exactly as a run's batch
    would. ``AnalyserError`` propagates, as in a run.
    """
    candidate = ports.work.reported(file_id)
    if candidate is None:
        logger.debug("stream_cue_request_done", file_id=str(file_id), outcome=_NONE_NEEDED)
        return
    run = _Run(_Backlog(ports.work, [], 1))
    _run_batch(ports, timing, run, [candidate])
    (outcome,) = run.counts
    log = logger.info if outcome in _STORED else logger.debug
    log("stream_cue_request_done", file_id=str(file_id), outcome=outcome)
