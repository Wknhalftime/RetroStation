"""The ending analysis run (EOM ending rules, stage 1, Task 6), over in-memory fakes.

One bounded run: purge other versions and prune audio without cues; then, batch by batch, for
each song that needs it, measure the loudness tail, classify the ending and store it, with a
commit per batch; stop after a batch once the time budget is spent; log one summary. A file
that cannot be read stores ``unmeasured``, so it is not retried every run. A missing or broken
FFmpeg ends the run, so it never marks the whole library ``unmeasured``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from structlog.testing import capture_logs

from backend.domain.endings import (
    ENDING_ANALYSER_VERSION,
    EndingCandidate,
    EndingType,
    LoudnessTail,
    SongEnding,
)
from backend.domain.library import AudioHash
from backend.domain.streaming import CuePoints
from backend.playout.loudness import LoudnessReadError, LoudnessToolError
from backend.services.streaming.ending_analysis import (
    EndingRunConfig,
    EndingRunPorts,
    run_ending_analysis,
)
from tests.fakes.stream_endings import FakeStreamEndingRepository

CUES = CuePoints(
    cue_in_ms=0,
    cue_out_ms=101_000,
    fade_in_ms=0,
    fade_out_ms=4_000,
    start_next_ms=1_200,
    gain_db=-6.0,
)
"""Autocue's EOM at 99.8 s."""

COLD_TAIL = LoudnessTail(
    41_000, 50, tuple(0.0 if ms < 100_000 else -40.0 for ms in range(41_000, 102_000, 50))
)
"""Full volume until 100 s, then a cliff: a cold ending for ``CUES``."""

FLAT_TAIL = LoudnessTail(41_000, 50, tuple(-1.0 for _ in range(41_000, 102_000, 50)))
"""Loud right up to the end, no cliff: neither cold nor a long tail."""


def audio(n: int) -> AudioHash:
    return AudioHash.parse("flac-md5:" + f"{n:032x}")


def seeded(count: int) -> FakeStreamEndingRepository:
    repo = FakeStreamEndingRepository()
    for n in range(count):
        repo.cued[audio(n)] = EndingCandidate(audio(n), f"/music/{n}.flac", CUES)
    return repo


class Clock:
    """A steady clock that moves on by ``step`` seconds at every read."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def ports(
    repo: FakeStreamEndingRepository,
    measure: Callable[[str, int], LoudnessTail],
    commits: list[int],
) -> EndingRunPorts:
    return EndingRunPorts(
        endings=repo, commit=lambda: commits.append(len(repo.events)), measure=measure
    )


def test_config_defaults() -> None:
    config = EndingRunConfig(steady=Clock())
    assert (config.budget_s, config.batch_size, config.unreadable_breaker) == (120.0, 8, 4)


def test_whole_batch_unreadable_stops_the_run() -> None:
    """Review Focus 4: a misconfigured FFmpeg fails every file. A batch of at least
    ``unreadable_breaker`` files that are all unreadable stores nothing and ends the run with
    ``LoudnessToolError``, so the library is never marked unmeasured wholesale."""
    repo = seeded(10)

    def measure(path: str, _: int) -> LoudnessTail:
        raise LoudnessReadError("ffmpeg exited 1")

    with pytest.raises(LoudnessToolError):
        run_ending_analysis(ports(repo, measure, []), EndingRunConfig(Clock()))
    assert repo.endings == {}


def test_small_unreadable_batch_is_stored_unmeasured() -> None:
    """Below the breaker (fewer than 4 files in the batch), unreadable files are stored
    ``unmeasured`` as usual, so the last few bad files of a library do not stall it."""
    repo = seeded(2)

    def measure(path: str, _: int) -> LoudnessTail:
        raise LoudnessReadError("corrupt")

    run_ending_analysis(ports(repo, measure, []), EndingRunConfig(Clock()))
    assert {e.ending for e in repo.endings.values()} == {EndingType.UNMEASURED}
    assert len(repo.endings) == 2


def test_run_stores_each_classified_ending() -> None:
    repo = seeded(3)
    tails = {"/music/0.flac": COLD_TAIL, "/music/1.flac": FLAT_TAIL, "/music/2.flac": COLD_TAIL}
    run_ending_analysis(ports(repo, lambda path, _: tails[path], []), EndingRunConfig(Clock()))
    assert repo.endings == {
        audio(0): SongEnding(EndingType.COLD, None, 101_000),
        audio(1): SongEnding(EndingType.OTHER, None, 101_000),
        audio(2): SongEnding(EndingType.COLD, None, 101_000),
    }
    assert set(repo.versions.values()) == {ENDING_ANALYSER_VERSION}


def test_measure_gets_path_and_stop() -> None:
    repo = seeded(1)
    calls: list[tuple[str, int]] = []

    def measure(path: str, cue_out_ms: int) -> LoudnessTail:
        calls.append((path, cue_out_ms))
        return COLD_TAIL

    run_ending_analysis(ports(repo, measure, []), EndingRunConfig(Clock()))
    assert calls == [("/music/0.flac", 101_000)]


def test_unreadable_file_stores_unmeasured() -> None:
    repo = seeded(2)

    def measure(path: str, _: int) -> LoudnessTail:
        if path == "/music/0.flac":
            raise LoudnessReadError("corrupt file")
        return COLD_TAIL

    run_ending_analysis(ports(repo, measure, []), EndingRunConfig(Clock()))
    assert repo.endings[audio(0)] == SongEnding(EndingType.UNMEASURED, None, 101_000)
    assert repo.endings[audio(1)].ending is EndingType.COLD


def test_tool_error_stops_run_and_keeps_earlier_batches() -> None:
    repo = seeded(5)
    commits: list[int] = []

    def measure(path: str, _: int) -> LoudnessTail:
        if path == "/music/3.flac":
            raise LoudnessToolError("ffmpeg not found")
        return COLD_TAIL

    with pytest.raises(LoudnessToolError):
        run_ending_analysis(ports(repo, measure, commits), EndingRunConfig(Clock(), batch_size=2))
    # The first batch (0, 1) is stored and committed; the failing batch (2, 3) and the rest
    # store nothing, and nothing is ever marked unmeasured.
    assert set(repo.endings) == {audio(0), audio(1)}
    assert commits and commits[-1] >= repo.events.index(f"store:{audio(1)}") + 1
    assert all(e.ending is not EndingType.UNMEASURED for e in repo.endings.values())


def test_run_purges_and_prunes_before_measuring() -> None:
    repo = seeded(1)
    measured_after: list[list[str]] = []

    def measure(path: str, _: int) -> LoudnessTail:
        measured_after.append(list(repo.events))
        return COLD_TAIL

    run_ending_analysis(ports(repo, measure, []), EndingRunConfig(Clock()))
    assert [set(events) for events in measured_after] == [{"purge", "prune"}]


def test_run_drops_old_versions_and_endings_without_cues() -> None:
    repo = seeded(1)
    repo.endings[audio(0)] = SongEnding(EndingType.OTHER, None, 101_000)
    repo.versions[audio(0)] = ENDING_ANALYSER_VERSION + 1  # another analyser's row
    gone = audio(42)
    repo.endings[gone] = SongEnding(EndingType.OTHER, None, 101_000)
    repo.versions[gone] = ENDING_ANALYSER_VERSION
    run_ending_analysis(ports(repo, lambda *_: COLD_TAIL, []), EndingRunConfig(Clock()))
    assert gone not in repo.endings
    assert repo.endings[audio(0)].ending is EndingType.COLD  # re-measured at this version


def test_commits_once_per_batch_at_least() -> None:
    repo = seeded(5)
    commits: list[int] = []
    run_ending_analysis(
        ports(repo, lambda *_: COLD_TAIL, commits), EndingRunConfig(Clock(), batch_size=2)
    )
    stores = [i for i, e in enumerate(repo.events) if e.startswith("store:")]
    # A commit falls between each batch's last store and the next batch's first store
    # (``commits`` holds the event count at each commit), and after the last store.
    for last, first_of_next in ((stores[1], stores[2]), (stores[3], stores[4])):
        assert any(last < c <= first_of_next for c in commits)
    assert commits[-1] == len(repo.events)


def test_budget_stops_after_a_batch() -> None:
    repo = seeded(6)
    run_ending_analysis(
        ports(repo, lambda *_: COLD_TAIL, []),
        EndingRunConfig(Clock(step=10.0), budget_s=15.0, batch_size=2),
    )
    assert 2 <= len(repo.endings) < 6
    assert len(repo.endings) % 2 == 0


def test_idle_run_logs_nothing() -> None:
    repo = seeded(0)
    with capture_logs() as logs:
        run_ending_analysis(ports(repo, lambda *_: COLD_TAIL, []), EndingRunConfig(Clock()))
    assert [e for e in logs if e["event"] == "stream_ending_run"] == []


def test_summary_counts_by_type() -> None:
    repo = seeded(3)
    tails = {"/music/0.flac": COLD_TAIL, "/music/1.flac": FLAT_TAIL}

    def measure(path: str, _: int) -> LoudnessTail:
        if path not in tails:
            raise LoudnessReadError("corrupt")
        return tails[path]

    with capture_logs() as logs:
        run_ending_analysis(ports(repo, measure, []), EndingRunConfig(Clock()))
    [summary] = [e for e in logs if e["event"] == "stream_ending_run"]
    assert summary["log_level"] == "info"
    assert (summary["cold"], summary["long_tail"], summary["other"], summary["unmeasured"]) == (
        1,
        0,
        1,
        1,
    )
