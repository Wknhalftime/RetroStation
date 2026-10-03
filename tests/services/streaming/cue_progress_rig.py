"""The cue progress rig (PR G2, Task 7): E1's locked cue rig (``cue_library.CueRig``) with a
recording progress write and a coverage read that follows the cue store.

Spec, Testing: "Service: in-memory fakes"; project rule: no sleep-based ordering (the progress
clock is a step clock, UTC-aware like every progress writer, review I5).

- ``writes`` records every progress row the run wrote, in order.
- ``coverage`` answers what ``FakeCueCoverageRepository`` counts over the files added here
  and the cue store; ``fail_next()`` makes the next read give ``None`` (a count that failed or
  timed out, I7); ``reads`` counts the reads.
- ``cue.events`` records, in order, each ``"analyse"``, each ``"commit"`` and each ``"count"``
  (a coverage read). On PostgreSQL the count runs on its own connection, so it sees only what
  the run has committed (audit SF1).
- ``run`` is one cue run with a real progress sink (``CueProgressRows``); ``report`` is one
  reported song (D79) through the same ports.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from structlog.testing import capture_logs

from backend.domain.library import AudioHash
from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    CueAnalysis,
    CueCandidate,
    CueCoverage,
    StreamTiming,
)
from backend.domain.system import TaskProgress
from backend.playout.cue_analysis import BatchAnalysis, CueFile
from backend.services.streaming.cue_precompute import (
    CueRunConfig,
    CueRunPorts,
    analyse_reported,
    run_cue_analysis,
)
from backend.services.streaming.cue_progress import CueProgressPorts, CueProgressRows
from tests.fakes.stream_cue_coverage import FakeCueCoverageRepository
from tests.services.streaming.cue_library import TODAY, CueRig, LogEvent
from tests.services.streaming.test_autocue import VAN_HALEN_POINTS

PROGRESS_START = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)

__all__ = [
    "PROGRESS_START",
    "CueProgressRig",
    "FollowingCoverage",
    "RecordingWrite",
    "StepClock",
]


@dataclass
class RecordingWrite:
    """A ``ProgressWrite`` that keeps every row it is given."""

    rows: list[TaskProgress] = field(default_factory=list)

    def __call__(self, task: TaskProgress) -> None:
        self.rows.append(task)


@dataclass
class FollowingCoverage:
    """A ``CoverageRead`` over the fake coverage repository."""

    repo: FakeCueCoverageRepository
    events: list[str] = field(default_factory=list)
    reads: int = 0
    _fail_next: bool = False

    def fail_next(self) -> None:
        self._fail_next = True

    def __call__(self) -> CueCoverage | None:
        self.reads += 1
        self.events.append("count")
        if self._fail_next:
            self._fail_next = False
            return None
        return self.repo.coverage()


@dataclass
class StepClock:
    """A UTC-aware clock that moves on one second at each reading."""

    now: datetime = PROGRESS_START

    def __call__(self) -> datetime:
        reading = self.now
        self.now += timedelta(seconds=1)
        return reading


class CueProgressRig:
    def __init__(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.cue = CueRig(folder)
        self.writes = RecordingWrite()
        self.coverage = FollowingCoverage(
            FakeCueCoverageRepository(self.cue.store), events=self.cue.events
        )
        self.clock = StepClock()
        self.logs: list[LogEvent] = []
        self._runs = 0

    def add(self, name: str, *, audio: AudioHash | None = None) -> CueCandidate:
        """A file that needs analysis (unless its audio already has a row)."""
        candidate = self.cue.add(name, audio=audio)
        self.coverage.repo.add(candidate.file_id, candidate.audio_hash)
        return candidate

    def add_settled(self, name: str, *, failed: bool) -> CueCandidate:
        """A file whose audio already has a row: real cues, or a fallback (``failed``)."""
        candidate = self.add(name)
        self.cue.store.upsert(
            CueAnalysis(
                audio_hash=candidate.audio_hash,
                cues=VAN_HALEN_POINTS,
                loudness_lufs=None,
                analysis_failed=failed,
                analyser_version=CUE_ANALYSER_VERSION,
            )
        )
        return candidate

    def add_unhashed(self, file_id: UUID) -> None:
        self.coverage.repo.add(file_id, None)

    def _ports(self) -> CueRunPorts:
        self._runs += 1
        progress = CueProgressRows(
            CueProgressPorts(
                write=self.writes,
                coverage=self.coverage,
                clock=self.clock,
                run_id=f"run-{self._runs}",
            )
        )
        return CueRunPorts(
            work=self.cue.work,
            store=self.cue.store,
            commit=lambda: self.cue.events.append("commit"),
            analyse=self._analyse,
            progress=progress,
        )

    def _analyse(self, batch: Sequence[CueFile]) -> BatchAnalysis:
        self.cue.events.append("analyse")
        return self.cue.analyser(batch)

    def run(self, *, batch_size: int = 8) -> LogEvent | None:
        """One run with a real progress sink; its ``stream_cue_run`` summary, or None. Every
        event the run logged is added to ``logs``."""
        config = CueRunConfig(
            today=TODAY,
            steady=lambda: self.cue.steady_now,
            batch_size=batch_size,
            timing=StreamTiming(),
        )
        ports = self._ports()
        with capture_logs() as logs:
            try:
                run_cue_analysis(ports, config)
            finally:
                self.logs.extend(logs)
        summaries = [e for e in logs if e["event"] == "stream_cue_run"]
        assert len(summaries) <= 1, summaries
        return summaries[0] if summaries else None

    def report(self, file_id: UUID) -> None:
        """One reported song (D79) through the same ports a run gets."""
        with capture_logs() as logs:
            analyse_reported(self._ports(), StreamTiming(), file_id)
        self.logs.extend(logs)
