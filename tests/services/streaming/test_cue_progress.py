"""The cue run's progress row (PR G2, Task 7; traceability N: T7.1-T7.9).

Requirements:
- D89: "cues ready X of Y" shows in the app-wide bottom bar, like scans and enrichment, through
  the existing ``progress_tracking`` / ``/ws`` / ``ProgressBar`` machinery (D77a), so the row
  carries the bar's ``processed`` and ``total``;
- D77 and PG7: the count is settled audio (ready or failed: both have a row, D20) of all the
  analysable audio; unhashed files are not counted;
- D59, superseded for the bar by D77a and D89, still holds where it counts: an idle run, or
  one that stores nothing, leaves no trace; the run keeps its one summary log (E1 review I2);
- I7: a count that fails or times out writes no row and changes nothing in the run;
- D79: a reported song is a report, not a run: no progress row;
- M12: each storing run has its own row; error handling: an analyser failure marks the row
  failed and the run still ends as before (the error reaches the task boundary).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from backend.domain.enums import TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.playout.errors import AnalyserError
from tests.services.streaming.cue_library import CueRig
from tests.services.streaming.cue_progress_rig import CueProgressRig

INFO_OR_ABOVE = {"info", "warning", "error", "critical"}


@pytest.fixture
def rig(tmp_path: Path) -> CueProgressRig:
    return CueProgressRig(tmp_path / "progress")


def plain_rig(tmp_path: Path) -> CueRig:
    """E1's rig, whose run has no progress sink (``SilentProgress``, the default)."""
    folder = tmp_path / "plain"
    folder.mkdir()
    return CueRig(folder)


def counts(rows: list[TaskProgress]) -> list[tuple[int, int]]:
    return [(row.progress_data["processed"], row.progress_data["total"]) for row in rows]


def statuses(rows: list[TaskProgress]) -> list[TaskStatus]:
    return [row.status for row in rows]


def test_a_run_that_stores_cues_shows_live_progress(rig: CueProgressRig) -> None:
    # T7.1 (D77a, D89): a cue-analysis row, updated after each batch that stores something;
    # the coverage is read once, after the first such batch.
    for name in ("a", "b", "c"):
        rig.add(f"{name}.flac")
    rig.run(batch_size=1)
    running = [row for row in rig.writes.rows if row.status == TaskStatus.RUNNING]
    assert counts(running) == [(1, 3), (2, 3), (3, 3)]
    assert {row.task_type for row in rig.writes.rows} == {TaskType.CUE_ANALYSIS}
    assert len({row.task_id for row in rig.writes.rows}) == 1
    # One count per storing run (design note 9; R3: the count may be slow when cold).
    assert rig.coverage.reads == 1
    # The count sees the first batch (audit SF1): it is read after that batch commits, as a
    # separate connection would see it.
    events = rig.cue.events
    first_analyse = events.index("analyse")
    committed = events.index("commit", first_analyse)
    assert events.index("count") > committed


def test_the_row_is_completed_when_the_run_ends(rig: CueProgressRig) -> None:
    # T7.2 (D89; M12): the run's one row ends COMPLETED, so the bar shows "Done" per run; the
    # next storing run has a row of its own.
    rig.add("a.flac")
    rig.add("b.flac")
    rig.run(batch_size=1)
    first = list(rig.writes.rows)
    assert statuses(first) == [TaskStatus.RUNNING, TaskStatus.RUNNING, TaskStatus.COMPLETED]
    last = first[-1]
    assert last.completed_at is not None and last.completed_at.tzinfo is not None
    assert last.updated_at.tzinfo is not None
    assert counts([last]) == [(2, 2)]
    assert len({row.started_at for row in first}) == 1
    rig.add("c.flac")
    rig.run()
    later = rig.writes.rows[len(first) :]
    assert statuses(later)[-1] == TaskStatus.COMPLETED
    assert {row.task_id for row in later}.isdisjoint({row.task_id for row in first})


def test_an_idle_run_writes_no_row_and_reads_no_counts(rig: CueProgressRig) -> None:
    # T7.3 (D59, I2), guard: nothing to analyse leaves no trace, not even a count.
    rig.run()
    assert rig.writes.rows == []
    assert rig.coverage.reads == 0


def test_a_run_that_stores_nothing_writes_no_row(rig: CueProgressRig) -> None:
    # T7.4 (I2): files that cannot be read store nothing (D57), so the run shows no bar.
    for name in ("a", "b"):
        Path(rig.add(f"{name}.flac").path).unlink()
    summary = rig.run(batch_size=1)
    assert summary is not None and summary["unreadable"] == 2
    assert rig.writes.rows == []
    assert rig.coverage.reads == 0


def test_an_analyser_failure_marks_the_row_failed_and_still_ends_the_run(
    rig: CueProgressRig,
) -> None:
    # T7.5 (error handling; D89): the bar shows the failure; the error still reaches the task
    # boundary, and the batch stored before it stays stored.
    first = rig.add("a.flac")
    rig.add("b.flac")
    rig.cue.analyser.fail_on_call = 2
    with pytest.raises(AnalyserError):
        rig.run(batch_size=1)
    assert statuses(rig.writes.rows) == [TaskStatus.RUNNING, TaskStatus.FAILED]
    failed = rig.writes.rows[-1]
    assert failed.completed_at is not None
    assert len({row.task_id for row in rig.writes.rows}) == 1
    assert set(rig.cue.store.analyses) == {first.audio_hash}


def test_a_reported_song_writes_no_progress_row(rig: CueProgressRig) -> None:
    # T7.6 (D79), guard: a report analyses one song ahead of the queue; it is not a run.
    reported = rig.add("a.flac")
    rig.report(reported.file_id)
    assert set(rig.cue.store.analyses) == {reported.audio_hash}
    assert rig.writes.rows == []
    assert rig.coverage.reads == 0


def test_the_run_keeps_its_one_summary_log(rig: CueProgressRig, tmp_path: Path) -> None:
    # T7.7 (D59, I2), guard: the progress row adds nothing to System Logs; the run's summary
    # is the one a run with no progress sink logs.
    plain = plain_rig(tmp_path)
    for name in ("a", "b", "c"):
        rig.add(f"{name}.flac")
        plain.add(f"{name}.flac")
    with_progress = rig.run(batch_size=2)
    assert with_progress is not None
    assert with_progress == plain.run(batch_size=2)
    loud = [e["event"] for e in rig.logs if e["log_level"] in INFO_OR_ABOVE]
    assert loud == ["stream_cue_run"]


def test_progress_counts_settled_audio_of_all_analysable(rig: CueProgressRig) -> None:
    # T7.8 (PG7, D77): audio with real cues and audio with a fallback row are both settled;
    # the files with no fingerprint are not part of the count.
    rig.add_settled("ready.flac", failed=False)
    rig.add_settled("fallback.flac", failed=True)
    rig.add("waiting-1.flac")
    rig.add("waiting-2.flac")
    rig.add_unhashed(uuid4())
    rig.run(batch_size=1)
    running = [row for row in rig.writes.rows if row.status == TaskStatus.RUNNING]
    assert counts(running) == [(3, 4), (4, 4)]


def test_a_count_that_fails_writes_no_row_and_changes_nothing(
    rig: CueProgressRig, tmp_path: Path
) -> None:
    # T7.9 (I7), guard: the count gave nothing (failed or timed out on its own connection),
    # so this run writes no row; what it stores and logs equal a run with no progress sink.
    plain = plain_rig(tmp_path)
    for name in ("a", "b"):
        rig.add(f"{name}.flac")
        plain.add(f"{name}.flac")
    rig.coverage.fail_next()
    summary = rig.run(batch_size=1)
    assert rig.writes.rows == []
    assert summary == plain.run(batch_size=1)
    assert len(rig.cue.store.analyses) == len(plain.store.analyses) == 2
