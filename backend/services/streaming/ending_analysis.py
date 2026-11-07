"""The ending analysis: one bounded run over the songs whose ending needs measuring.

Spec: docs/superpowers/specs/2026-11-07-eom-ending-rules-design.md; plan
docs/superpowers/plans/2026-11-07-eom-ending-rules-stage1.md (Task 6). A separate run from
autocue's (``cue_precompute``), on the same cue worker, with its own table and version, so an
ending change never re-runs autocue.

The pipeline, one function per step:

1. purge the other analyser versions and prune endings whose audio lost its cues, and commit;
2. take a batch of songs by keyset (``needing_analysis``) and commit, so no transaction sits
   idle while FFmpeg decodes;
3. measure and classify the whole batch, then store it: an unreadable file stores
   ``unmeasured`` (not retried every run); FFmpeg missing ends the run; a batch of at least
   ``unreadable_breaker`` files that are all unreadable is a misconfigured FFmpeg, so it stores
   nothing and ends the run too, rather than marking the whole library ``unmeasured``;
4. commit, and stop after a batch once the time budget is spent;
5. log one ``stream_ending_run`` summary, unless the run measured nothing.

``measure`` blocks for as long as a decode takes, so a run belongs on the cue worker.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import structlog

from backend.domain.endings import (
    DEFAULT_RULES,
    ENDING_ANALYSER_VERSION,
    EndingCandidate,
    EndingRules,
    EndingType,
    LoudnessTail,
    SongEnding,
    classify_ending,
)
from backend.domain.library import AudioHash
from backend.playout.loudness import LoudnessReadError, LoudnessToolError
from backend.repositories.stream_endings import StreamEndingRepository

logger = structlog.get_logger()

type MeasureTail = Callable[[str, int], LoudnessTail]
"""playout's ``read_tail`` with its settings bound: (path, autocue's Stop) -> the tail."""


@dataclass(frozen=True)
class EndingRunPorts:
    endings: StreamEndingRepository
    commit: Callable[[], None]
    measure: MeasureTail


@dataclass(frozen=True)
class EndingRunConfig:
    steady: Callable[[], float]
    budget_s: float = 120.0
    batch_size: int = 8
    unreadable_breaker: int = 4
    rules: EndingRules = field(default=DEFAULT_RULES)

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError(f"EndingRunConfig.batch_size must be >= 1, got {self.batch_size}")
        if not self.budget_s > 0:  # NaN included
            raise ValueError(f"EndingRunConfig.budget_s must be > 0, got {self.budget_s}")


@dataclass(frozen=True)
class _Measured:
    audio_hash: AudioHash
    ending: SongEnding
    error: str | None = None  # why the file could not be read, for an unmeasured ending


def _tidy(ports: EndingRunPorts) -> None:
    """Purge other versions and prune endings without cues, before anything is measured."""
    ports.endings.purge_other_versions(ENDING_ANALYSER_VERSION)
    ports.endings.prune_without_cues()
    ports.commit()


def _measure_one(ports: EndingRunPorts, rules: EndingRules, song: EndingCandidate) -> _Measured:
    """One song's ending; ``unmeasured`` when its file cannot be read. ``LoudnessToolError``
    propagates."""
    try:
        tail = ports.measure(song.path, song.cues.cue_out_ms)
    except LoudnessReadError as error:
        logger.debug("stream_ending_unreadable", path=song.path, error=str(error))
        unmeasured = SongEnding(EndingType.UNMEASURED, None, song.cues.cue_out_ms)
        return _Measured(song.audio_hash, unmeasured, str(error))
    return _Measured(song.audio_hash, classify_ending(tail, song.cues, rules))


def _check_breaker(measured: Sequence[_Measured], config: EndingRunConfig) -> None:
    """A batch of at least ``unreadable_breaker`` files that are all unreadable means FFmpeg is
    misconfigured, not that every file is bad: end the run before storing any of them."""
    if len(measured) >= config.unreadable_breaker and all(m.error for m in measured):
        raise LoudnessToolError(
            f"every file in a batch of {len(measured)} was unreadable; "
            f"the first: {measured[0].error}"
        )


def _run_batch(
    ports: EndingRunPorts, config: EndingRunConfig, batch: Sequence[EndingCandidate]
) -> list[_Measured]:
    """Measure the batch, then store and commit it; what was stored."""
    ports.commit()  # no transaction sits idle during the decodes
    measured = [_measure_one(ports, config.rules, song) for song in batch]
    _check_breaker(measured, config)
    for one in measured:
        ports.endings.store(one.audio_hash, one.ending)
    ports.commit()
    return measured


def _log_summary(counts: Counter[EndingType], paused: bool) -> None:
    if not counts:
        return
    logger.info(
        "stream_ending_run",
        cold=counts[EndingType.COLD],
        long_tail=counts[EndingType.LONG_TAIL],
        other=counts[EndingType.OTHER],
        unmeasured=counts[EndingType.UNMEASURED],
        paused=paused,
    )


def run_ending_analysis(ports: EndingRunPorts, config: EndingRunConfig) -> None:
    """One bounded run. Its results are the store's state and one ``stream_ending_run`` log.

    ``LoudnessToolError`` propagates to the task boundary; the batches before it are committed.
    """
    started = config.steady()
    _tidy(ports)
    counts: Counter[EndingType] = Counter()
    cursor: AudioHash | None = None
    paused = False
    while batch := ports.endings.needing_analysis(cursor, config.batch_size):
        cursor = batch[-1].audio_hash
        counts.update(m.ending.ending for m in _run_batch(ports, config, batch))
        if config.steady() - started >= config.budget_s:
            paused = True
            break
    _log_summary(counts, paused)
