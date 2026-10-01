"""A cue pre-computation run (spec: Cue pre-computation; D20 purge; D52 fallback; D55 no
duration retried; D57 unreadable and changed; D61 stalls; D62 priority; D63 batches of 8 and
a bounded run; D64 unknown stat; D67 each file's duration reaches the analyser; D68 a malformed
result fails only its file; review I2: the run returns nothing, and its results are the
store's state and its summary log; review I4: the priority set is read once per run).
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from backend.domain.streaming import CUE_ANALYSER_VERSION, CueAnalysis
from backend.playout.cue_analysis import CueFile
from backend.playout.errors import AnalyserError
from tests.services.streaming.cue_library import PRIORITY_DAYS, CueRig, new_audio, on_disk
from tests.services.streaming.test_autocue import VAN_HALEN_POINTS


@pytest.fixture
def rig(tmp_path: Path) -> CueRig:
    return CueRig(tmp_path)


def test_files_playing_on_each_priority_day_are_analysed_first(rig: CueRig) -> None:
    """D62: today's and tomorrow's playlists for every station-year first, then the rest
    (path order alone would take the a- and b- files first)."""
    rest = [
        rig.add("a-unscheduled.flac"),
        rig.add("b-another-day.flac", playing_on=[date(1995, 3, 16)]),
    ]
    first = [rig.add(f"z{i}.flac", playing_on=[day]) for i, day in enumerate(PRIORITY_DAYS)]
    rig.run()
    order = rig.analyser.order()
    assert set(order[:4]) == {c.path for c in first}
    assert set(order[4:]) == {c.path for c in rest}


def test_each_audio_is_analysed_once_when_files_share_it(rig: CueRig) -> None:
    """Twins share one hash and one cue row: the audio is analysed once."""
    shared = new_audio()
    rig.add("a.flac", audio=shared, playing_on=[PRIORITY_DAYS[0]])
    rig.add("b.flac", audio=shared, playing_on=[PRIORITY_DAYS[0]])
    rig.run()
    assert len(rig.analyser.order()) == 1
    assert set(rig.store.analyses) == {shared}


def test_each_batch_is_committed_before_the_next_is_analysed(rig: CueRig) -> None:
    """Backfill pattern: bounded, committed batches, so a crash loses one batch at most."""
    for name in ("a", "b", "c"):
        rig.add(f"{name}.flac")
    rig.run(batch_size=1)
    analyses = [i for i, event in enumerate(rig.events) if event == "analyse"]
    assert len(analyses) == 3
    assert all(rig.events[i + 1] == "commit" for i in analyses)


def test_a_file_that_cannot_be_opened_is_counted_and_the_rest_continue(rig: CueRig) -> None:
    """D57: an unreadable file stores nothing and is retried next run; Review Focus 1 (the
    share comes back with the same bytes and mtime, and the next run analyses it)."""
    gone = rig.add("a-gone.flac")
    content = Path(gone.path).read_bytes()
    Path(gone.path).unlink()
    kept = rig.add("b.flac")
    summary = rig.run()
    assert summary is not None and summary["unreadable"] == 1
    assert set(rig.store.analyses) == {kept.audio_hash}
    assert gone.path not in rig.analyser.order()
    Path(gone.path).write_bytes(content)
    assert gone.file_mtime_ns is not None
    os.utime(gone.path, ns=(gone.file_mtime_ns, gone.file_mtime_ns))
    again = rig.run()
    assert again is not None and (again["analysed"], again["unreadable"]) == (1, 0)
    assert set(rig.store.analyses) == {kept.audio_hash, gone.audio_hash}


def test_a_file_changed_since_it_was_hashed_is_not_analysed(rig: CueRig) -> None:
    """D57: a stat that differs from the row's before analysis stores nothing, counted
    changed."""
    changed = rig.add("a.flac")
    Path(changed.path).write_bytes(b"new audio, new size" * 10)
    summary = rig.run()
    assert summary is not None and summary["changed"] == 1
    assert (rig.analyser.calls, rig.store.analyses) == ([], {})


def test_a_file_rewritten_while_analysed_is_not_stored(rig: CueRig) -> None:
    """D57: a stat that differs after analysis stores nothing, counted changed."""
    target = rig.add("a.flac")
    rig.analyser.during = lambda paths: Path(target.path).write_bytes(b"rewritten" * 50)
    summary = rig.run()
    assert summary is not None and summary["changed"] == 1
    assert rig.store.analyses == {}


@pytest.mark.parametrize(
    "unknown_fields",
    [("file_size",), ("file_mtime_ns",), ("file_size", "file_mtime_ns")],
    ids=["size unknown", "mtime unknown", "both unknown"],
)
def test_an_unknown_stored_stat_trusts_the_hash(
    rig: CueRig, unknown_fields: tuple[str, ...]
) -> None:
    """D64: a NULL file_size or file_mtime_ns skips the stat comparison (before and after),
    not only the unknown field's; only the conditional hash check applies. The rewrite
    changes both the size and the mtime, so comparing the known field alone would see it."""
    known = on_disk(rig.folder, "a.flac")
    unknown = replace(known, **dict.fromkeys(unknown_fields))
    rig.work.add(unknown)

    def rewrite(paths: object) -> None:
        Path(known.path).write_bytes(b"rewritten" * 50)
        assert known.file_mtime_ns is not None
        later = known.file_mtime_ns + 5_000_000_000
        os.utime(known.path, ns=(later, later))

    rig.analyser.during = rewrite
    rig.run()
    assert set(rig.store.analyses) == {unknown.audio_hash}


def test_a_file_the_analyser_stalls_on_gets_a_fallback_row_and_the_rest_are_analysed(
    rig: CueRig,
) -> None:
    """D61: a file that crashes or hangs the analyser gets a fallback row; the batch's
    remaining files are analysed in the same run (Review Focus 2)."""
    a, b, c = (rig.add(f"{name}.flac") for name in ("a", "b", "c"))
    rig.analyser.stall_on = {b.path}
    rig.run(batch_size=3)
    assert rig.analyser.order() == [a.path, b.path, c.path, c.path]
    assert rig.store.analyses[b.audio_hash].analysis_failed is True
    assert rig.store.analyses[a.audio_hash].analysis_failed is False
    assert rig.store.analyses[c.audio_hash].analysis_failed is False


def test_a_malformed_result_gets_a_fallback_row_and_the_rest_are_analysed(
    rig: CueRig,
) -> None:
    """D68: a malformed result line fails only its file: the error is logged naming the
    field, that file gets a fallback row (as D61), and the rest of the batch is analysed."""
    a, b, c = (rig.add(f"{name}.flac") for name in ("a", "b", "c"))
    reason = "metadata on output line 7 must map strings to strings: {'a': 0.4}"
    rig.analyser.rejects = {b.path: reason}
    summary = rig.run(batch_size=3)
    assert rig.analyser.order() == [a.path, b.path, c.path]
    assert [rig.store.analyses[x.audio_hash].analysis_failed for x in (a, b, c)] == [
        False,
        True,
        False,
    ]
    assert summary is not None and (summary["analysed"], summary["failed"]) == (2, 1)
    [logged] = [e for e in rig.logs if e["event"] == "stream_cue_analysis_failed"]
    assert (logged["path"], logged["reason"]) == (b.path, reason)


def test_the_analyser_is_told_each_files_duration(rig: CueRig) -> None:
    """D67: the per-file deadline scales with the duration, so each file's duration_ms
    reaches the analyser (None when unknown)."""
    long = rig.add("a.flac", duration_ms=1_020_000)
    unknown = rig.add("b.flac", duration_ms=None)
    rig.run()
    assert rig.analyser.files == [CueFile(long.path, 1_020_000), CueFile(unknown.path, None)]


def test_nothing_is_stored_when_the_hash_changed_during_analysis(rig: CueRig) -> None:
    """For PR D and PR E: "stores nothing if the hash changed meanwhile"; Review Focus 3."""
    target = rig.add("a.flac")

    def retag(paths: object) -> None:
        rig.store.file_hashes[target.file_id] = None

    rig.analyser.during = retag
    summary = rig.run()
    assert summary is not None and summary["changed"] == 1
    assert rig.store.analyses == {}


def test_a_failure_without_a_duration_is_retried_on_the_next_run(rig: CueRig) -> None:
    """D55: no skip list; the file is analysed again by every run."""
    retried = rig.add("a.flac", duration_ms=None)
    rig.analyser.results = {retried.path: {}}
    first = rig.run()
    second = rig.run()
    assert rig.analyser.order() == [retried.path, retried.path]
    assert rig.store.analyses == {}
    assert first is not None and second is not None
    assert (first["no_duration"], second["no_duration"]) == (1, 1)


def test_the_run_stops_when_its_time_budget_is_spent(rig: CueRig) -> None:
    """D63 with N2: a run ends within the resume cadence; the steady clock (D47) times it."""
    for name in ("a", "b", "c", "d"):
        rig.add(f"{name}.flac")

    def spend(paths: object) -> None:
        rig.steady_now += 100.0

    rig.analyser.during = spend
    summary = rig.run(batch_size=1, budget_s=150.0)
    assert len(rig.analyser.calls) == 2
    assert summary is not None and summary["paused"] is True


def test_old_analyser_versions_are_purged_before_anything_is_analysed(rig: CueRig) -> None:
    """D20: "an analyser change purges its old rows once"; the audio is then analysed anew."""
    old = rig.add("a.flac")
    rig.store.upsert(
        CueAnalysis(
            audio_hash=old.audio_hash,
            cues=VAN_HALEN_POINTS,
            loudness_lufs=None,
            analysis_failed=False,
            analyser_version=CUE_ANALYSER_VERSION + 1,
        )
    )
    rig.run()
    assert rig.store.analyses[old.audio_hash].analyser_version == CUE_ANALYSER_VERSION


def test_an_idle_run_starts_no_analyser_and_logs_no_summary(rig: CueRig) -> None:
    """D59 and the review minor: a run every 5 minutes with nothing to do leaves no trace."""
    assert rig.run() is None
    assert rig.analyser.calls == []


def test_the_run_logs_what_it_did(rig: CueRig) -> None:
    """D59: no progress rows; the run's counts are one summary event (System Logs)."""
    rig.add("a.flac")
    failed = rig.add("b.flac")
    no_duration = rig.add("c.flac", duration_ms=None)
    gone = rig.add("d.flac")
    Path(gone.path).unlink()
    rig.analyser.results = {failed.path: {}, no_duration.path: {}}
    summary = rig.run()
    assert summary is not None
    keys = ("analysed", "failed", "no_duration", "changed", "unreadable", "paused")
    assert {k: summary[k] for k in keys} == {
        "analysed": 1,
        "failed": 1,
        "no_duration": 1,
        "changed": 0,
        "unreadable": 1,
        "paused": False,
    }


def test_an_analyser_that_cannot_run_ends_the_run_keeping_what_was_committed(
    rig: CueRig,
) -> None:
    """Never skip error handling: the error reaches the task boundary, and the batches
    before it stay stored."""
    first = rig.add("a.flac")
    rig.add("b.flac")
    rig.analyser.fail_on_call = 2
    with pytest.raises(AnalyserError):
        rig.run(batch_size=1)
    assert set(rig.store.analyses) == {first.audio_hash}
    assert "commit" in rig.events[1:-1]
