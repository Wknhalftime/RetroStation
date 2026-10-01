"""Analysing a reported song's audio ahead of the backlog (spec: D79, "The owner (E1's
pipeline) analyses that audio ahead of its queue, storing cues or a failed row as E1 does";
E1's rules carried: D20 (the hash re-read), D55, D57, D61, D64; E1 review I2: no info log
unless a row was stored)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from backend.domain.streaming import CUE_ANALYSER_VERSION, CueAnalysis, CuePoints, StreamTiming
from backend.playout.errors import AnalyserError
from backend.services.streaming.cue_precompute import CueRunPorts, analyse_reported
from backend.services.streaming.cue_record import FALLBACK_GAIN_DB
from tests.services.streaming.cue_library import PRIORITY_DAYS, CueRig, LogEvent, new_audio
from tests.services.streaming.test_autocue import VAN_HALEN_POINTS


@pytest.fixture
def rig(tmp_path: Path) -> CueRig:
    return CueRig(tmp_path)


def report(rig: CueRig, file_id: UUID) -> list[LogEvent]:
    """One reported file through the owner's pipeline; every event it logged."""
    ports = CueRunPorts(
        work=rig.work,
        store=rig.store,
        commit=lambda: rig.events.append("commit"),
        analyse=rig.analyser,
    )
    with capture_logs() as logs:
        analyse_reported(ports, StreamTiming(), file_id)
    return [dict(e) for e in logs]


def done(logs: Sequence[LogEvent]) -> list[tuple[object, object]]:
    return [(e["log_level"], e["outcome"]) for e in logs if e["event"] == "stream_cue_request_done"]


def test_a_reported_file_is_analysed_ahead_of_the_backlog(rig: CueRig) -> None:
    """D79 "ahead of its queue": only the reported file is analysed; the priority set (D62)
    and the library wait for the next run, which a report does not start."""
    rig.add("a.flac", playing_on=[PRIORITY_DAYS[0]])
    rig.add("b.flac")
    reported = rig.add("z.flac")
    report(rig, reported.file_id)
    assert rig.analyser.order() == [reported.path]
    assert list(rig.store.analyses) == [reported.audio_hash]
    assert rig.work.scheduled_days == []


def test_a_report_stores_what_a_run_stores_for_the_same_result(rig: CueRig) -> None:
    """D79 "as E1 does": one write path, so a report and a run store the same row."""
    reported = rig.add("reported.flac")
    backlog = rig.add("backlog.flac")
    report(rig, reported.file_id)
    rig.run()
    by_report = rig.store.analyses[reported.audio_hash]
    by_run = rig.store.analyses[backlog.audio_hash]
    assert by_report == replace(by_run, audio_hash=reported.audio_hash)
    assert by_report == CueAnalysis(
        reported.audio_hash, VAN_HALEN_POINTS, None, False, CUE_ANALYSER_VERSION
    )


@pytest.mark.parametrize("failed", [False, True], ids=["analysed", "failed row"])
def test_audio_that_already_has_a_row_is_not_analysed_again(rig: CueRig, failed: bool) -> None:
    """D79: the owner analyses only audio with no data. D61: a failed row is not analysed
    again until the analyser version changes."""
    reported = rig.add("song.flac")
    rig.store.upsert(
        CueAnalysis(reported.audio_hash, VAN_HALEN_POINTS, None, failed, CUE_ANALYSER_VERSION)
    )
    logs = report(rig, reported.file_id)
    assert rig.analyser.calls == []
    assert done(logs) == [("debug", "none_needed")]


def test_a_reported_file_that_cannot_be_opened_stores_nothing(rig: CueRig) -> None:
    """D57: an unreadable file stores nothing; a later run retries it."""
    reported = rig.add("gone.flac")
    Path(reported.path).unlink()
    logs = report(rig, reported.file_id)
    assert (rig.analyser.calls, rig.store.analyses) == ([], {})
    assert done(logs) == [("debug", "unreadable")]


def test_a_reported_file_changed_since_it_was_hashed_stores_nothing(rig: CueRig) -> None:
    """D57: a stat that differs from the row's stores nothing and counts as changed."""
    reported = rig.add("retagged.flac")
    Path(reported.path).write_bytes(b"\x01" * 4096)
    logs = report(rig, reported.file_id)
    assert (rig.analyser.calls, rig.store.analyses) == ([], {})
    assert done(logs) == [("debug", "changed")]


def test_an_unknown_stored_stat_trusts_the_hash(rig: CueRig) -> None:
    """D64: a NULL stored size or mtime skips the stat comparison."""
    known = rig.add("unknown.flac")
    unknown = replace(known, file_size=None, file_mtime_ns=None)
    rig.work.add(unknown)
    Path(unknown.path).write_bytes(b"\x01" * 4096)
    report(rig, unknown.file_id)
    assert list(rig.store.analyses) == [unknown.audio_hash]


def test_nothing_is_stored_when_the_hash_changed_during_the_analysis(rig: CueRig) -> None:
    """Spec: "reads the hash before analysing and stores nothing if the hash changed
    meanwhile"."""
    reported = rig.add("song.flac")

    def rehash(paths: Sequence[str]) -> None:
        rig.store.file_hashes[reported.file_id] = new_audio()

    rig.analyser.during = rehash
    logs = report(rig, reported.file_id)
    assert (rig.store.analyses, done(logs)) == ({}, [("debug", "changed")])


def test_a_reported_file_the_analyser_stalls_on_gets_a_fallback_row(rig: CueRig) -> None:
    """D61 with D52's fallback row; logged at info because a row was stored."""
    reported = rig.add("hangs.flac")
    rig.analyser.stall_on = {reported.path}
    logs = report(rig, reported.file_id)
    row = rig.store.analyses[reported.audio_hash]
    assert row.analysis_failed
    assert row.cues == CuePoints(0, 200_000, 3_000, 4_000, 0, FALLBACK_GAIN_DB)
    assert done(logs) == [("info", "failed")]


def test_a_reported_failure_without_a_duration_stores_nothing(rig: CueRig) -> None:
    """D55: no fallback row is possible without a duration; a later run retries it."""
    reported = rig.add("short.flac", duration_ms=None)
    rig.analyser.results = {reported.path: {}}
    logs = report(rig, reported.file_id)
    assert (rig.store.analyses, done(logs)) == ({}, [("debug", "no_duration")])


def test_a_report_logs_one_event_at_info_only_when_it_stored(rig: CueRig) -> None:
    """E1 review I2: System Logs hears about a report only when it stored a row."""
    reported = rig.add("song.flac")
    stored = report(rig, reported.file_id)
    again = report(rig, reported.file_id)
    assert (done(stored), done(again)) == ([("info", "analysed")], [("debug", "none_needed")])
    [event] = [e for e in stored if e["event"] == "stream_cue_request_done"]
    assert event["file_id"] == str(reported.file_id)


def test_an_analyser_that_cannot_run_ends_the_report(rig: CueRig) -> None:
    """As in a run: an AnalyserError reaches the task boundary, and nothing is stored."""
    reported = rig.add("song.flac")
    rig.analyser.fail_on_call = 1
    with pytest.raises(AnalyserError):
        report(rig, reported.file_id)
    assert rig.store.analyses == {}
