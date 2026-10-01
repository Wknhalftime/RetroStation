"""The cue pre-computation rig: files on disk seen as candidates, the fakes, a fake analyser
and the run's summary log (spec, Testing: "Service: in-memory fakes"; no sleeps: the steady
clock is a value the fake analyser advances)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from uuid import uuid4

from structlog.testing import capture_logs

from backend.domain.library import AudioHash
from backend.domain.streaming import CueCandidate, StreamTiming
from backend.playout.cue_analysis import BatchAnalysis, CueFile
from backend.playout.errors import AnalyserError
from backend.services.streaming.cue_precompute import CueRunConfig, CueRunPorts, run_cue_analysis
from tests.fakes.stream_cue_work import FakeCueWorkRepository
from tests.fakes.stream_cues import FakeStreamCueRepository
from tests.services.streaming.test_autocue import VAN_HALEN

TODAY = date(2026, 3, 14)
YEARS = range(1995, 1997)
PRIORITY_DAYS = (date(1995, 3, 14), date(1995, 3, 15), date(1996, 3, 14), date(1996, 3, 15))
"""tune_in_days(TODAY, YEARS)."""

type LogEvent = dict[str, object]


def new_audio() -> AudioHash:
    return AudioHash.parse(f"flac-md5:{uuid4().hex}")


def on_disk(
    folder: Path, name: str, *, audio: AudioHash | None = None, duration_ms: int | None = 200_000
) -> CueCandidate:
    """A file written to ``folder``, seen as a candidate with its current stat."""
    path = folder / name
    path.write_bytes(b"\x00" * 64 + name.encode())
    st = path.stat()
    return CueCandidate(
        file_id=uuid4(),
        path=str(path),
        audio_hash=audio or new_audio(),
        duration_ms=duration_ms,
        file_size=st.st_size,
        file_mtime_ns=st.st_mtime_ns,
    )


@dataclass
class FakeAnalyser:
    """Stands in for playout's ``analyse_batch``: autocue returns ``VAN_HALEN`` for a path
    unless ``results`` says otherwise; a ``stall_on`` path stops the batch there; a
    ``rejects`` path had a malformed result line (D68), with the reason naming the field."""

    results: dict[str, Mapping[str, str]] = field(default_factory=dict)
    stall_on: set[str] = field(default_factory=set)
    rejects: dict[str, str] = field(default_factory=dict)
    during: Callable[[Sequence[str]], None] | None = None
    fail_on_call: int | None = None
    calls: list[list[str]] = field(default_factory=list)
    files: list[CueFile] = field(default_factory=list)

    def __call__(self, batch: Sequence[CueFile]) -> BatchAnalysis:
        paths = [f.path for f in batch]
        self.calls.append(paths)
        self.files.extend(batch)
        if self.fail_on_call == len(self.calls):
            raise AnalyserError("cue analyser exit code 3: no such file")
        if self.during is not None:
            self.during(paths)
        metadata: dict[int, Mapping[str, str]] = {}
        rejected: dict[int, str] = {}
        for index, path in enumerate(paths):
            if path in self.stall_on:
                return BatchAnalysis(metadata=metadata, stalled=index, rejected=rejected)
            if path in self.rejects:
                rejected[index] = self.rejects[path]
            else:
                metadata[index] = self.results.get(path, VAN_HALEN)
        return BatchAnalysis(metadata=metadata, stalled=None, rejected=rejected)

    def order(self) -> list[str]:
        """Every path analysed, in the order analysed (review I3: not the batching)."""
        return [path for call in self.calls for path in call]


@dataclass
class CueRig:
    folder: Path
    store: FakeStreamCueRepository = field(default_factory=FakeStreamCueRepository)
    analyser: FakeAnalyser = field(default_factory=FakeAnalyser)
    events: list[str] = field(default_factory=list)
    logs: list[LogEvent] = field(default_factory=list)
    steady_now: float = 0.0
    work: FakeCueWorkRepository = field(init=False)

    def __post_init__(self) -> None:
        self.work = FakeCueWorkRepository(self.store)
        self.work.years = YEARS

    def add(
        self,
        name: str,
        *,
        playing_on: Iterable[date] = (),
        audio: AudioHash | None = None,
        duration_ms: int | None = 200_000,
    ) -> CueCandidate:
        candidate = on_disk(self.folder, name, audio=audio, duration_ms=duration_ms)
        self.work.add(candidate, playing_on=playing_on)
        return candidate

    def _analyse(self, batch: Sequence[CueFile]) -> BatchAnalysis:
        self.events.append("analyse")
        return self.analyser(batch)

    def _commit(self) -> None:
        self.events.append("commit")

    def run(self, *, batch_size: int = 8, budget_s: float = 240.0) -> LogEvent | None:
        """One run; its ``stream_cue_run`` summary event, or None when it logged none.
        Every event the run logged is added to ``logs``."""
        ports = CueRunPorts(
            work=self.work, store=self.store, commit=self._commit, analyse=self._analyse
        )
        config = CueRunConfig(
            today=TODAY,
            steady=lambda: self.steady_now,
            batch_size=batch_size,
            budget_s=budget_s,
            timing=StreamTiming(),
        )
        with capture_logs() as logs:
            run_cue_analysis(ports, config)
        self.logs.extend(logs)
        summaries = [e for e in logs if e["event"] == "stream_cue_run"]
        assert len(summaries) <= 1, summaries
        return summaries[0] if summaries else None
