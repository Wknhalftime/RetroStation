"""Acceptance tests: a silent file is recorded silent, not as a failure (D111; S1, S5).

Spec 2026-10-16-silent-files-design.md S1 and S5, building on the D111 proposal
(2026-10-06-silent-file-ruling.md, tests 1-5), the streaming spec's D52, D55, D59, D61, D79
and D89a:
- autocue.internal sets liq_amplify to lufs_target - lufs, and ebur128 reports -70 LUFS (its
  absolute gate) for audio with no block loud enough to count. At the -18 LUFS target that is
  +52.0 dB, computed from the target (never a bare 52). That gain alone is the verdict
  "silent", read before the cue points are checked: the silent stub fails CuePoints, and
  silent audio with valid full-length cues is silent too (S4b).
- A silent verdict stores D52's fallback row with analysis_failed and audio_silent, outcome
  CueRecord.SILENT, and one stream_cue_analysis_silent warning ("no audio (silent file)")
  once the row is stored; never stream_cue_analysis_failed.
- A stored row is never analysed again (D61). The run summary counts silent apart from failed
  (S5); the cue meter still counts the row as processed and failed (D89a).
- Any audible stretch is not silent: a quiet song is cued as now.

SILENT_STUB is rebuilt from the logged failure of the real stubs ("CuePoints.start_next_ms
(6000) must be less than the span (6000)") with the gain the proposal measured ("52.0 dB");
no recording of it exists in the repo. SILENT_FULL has the shape of the 3 analysed dev rows at
+52.0 dB (cue-in 0, cue-out the full length, a 0.4 s fade). test_silent_files_live.py checks
the real engine.
TESTIMONY is shaped on the proposal's measurement of Mary J. Blige - Testimony (cue-in 188.5 s,
cue-out 200.2 s, -6.079 dB); its fades are illustrative. New names are read as attributes
(autocue.Silent, CueRecord.SILENT, ...), so before the build each test fails on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from backend.domain.library import AudioHash
from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    CueAnalysis,
    CueCandidate,
    CueCoverage,
    CuePoints,
    StreamTiming,
)
from backend.playout.cue_analysis import AUTOCUE_SETTINGS
from backend.services.streaming import autocue
from backend.services.streaming.cue_precompute import (
    CueRunConfig,
    CueRunPorts,
    analyse_reported,
    run_cue_analysis,
)
from backend.services.streaming.cue_record import Autocued, CueRecord, record_analysis
from tests.fakes.stream_cue_coverage import FakeCueCoverageRepository
from tests.fakes.stream_cues import FakeStreamCueRepository
from tests.services.streaming.cue_library import TODAY, CueRig
from tests.services.streaming.test_autocue import VAN_HALEN, VAN_HALEN_POINTS

AUDIO = AudioHash.parse("flac-md5:" + "fedcba9876543210" * 2)
FILE = uuid4()
SILENT = "no audio (silent file)"

SILENT_STUB: dict[str, str] = {
    "liq_autocue": "internal",
    "liq_amplify": "52.0 dB",
    "liq_cue_in": "0.0",
    "liq_cue_out": "6.0",
    "liq_cross_start_duration": "0.0",
    "liq_cross_max_start_duration": "6.0",
    "liq_cross_end_duration": "6.0",
    "liq_fade_in": "0.0",
    "liq_fade_out": "6.0",
    "liq_fade_out_start_next": "0.0",
    "liq_fade_out_delay": "0.0",
}
SILENT_FULL: dict[str, str] = {  # the 3 analysed rows at +52.0 dB on dev (Slipknot - -Silent-)
    **SILENT_STUB,
    "liq_cue_out": "120.0",
    "liq_cross_max_start_duration": "0.4",
    "liq_cross_end_duration": "0.4",
    "liq_fade_out": "0.4",
}
TESTIMONY: dict[str, str] = {
    "liq_autocue": "internal",
    "liq_amplify": "-6.079 dB",
    "liq_cue_in": "188.5",
    "liq_cue_out": "200.2",
    "liq_cross_start_duration": "0.0",
    "liq_cross_max_start_duration": "11.7",
    "liq_cross_end_duration": "1.0",
    "liq_fade_in": "0.0",
    "liq_fade_out": "1.0",
    "liq_fade_out_start_next": "0.0",
    "liq_fade_out_delay": "0.0",
}
TESTIMONY_POINTS = CuePoints(
    cue_in_ms=188_500,
    cue_out_ms=200_200,
    fade_in_ms=0,
    fade_out_ms=1_000,
    start_next_ms=1_000,
    gain_db=-6.079,
)
FALLBACK = CuePoints(
    cue_in_ms=0,
    cue_out_ms=200_000,
    fade_in_ms=3_000,
    fade_out_ms=4_000,
    start_next_ms=0,
    gain_db=-8.0,
)
_TARGET_LINE = re.compile(
    r"^settings\.autocue\.internal\.lufs_target\s*:=\s*(-?\d+(?:\.\d*)?)\s*$", re.MULTILINE
)


def candidate(duration_ms: int | None = 200_000) -> CueCandidate:
    return CueCandidate(
        file_id=FILE,
        path="D:/Music/silent.flac",
        audio_hash=AUDIO,
        duration_ms=duration_ms,
        file_size=1,
        file_mtime_ns=2,
    )


def store_with_file() -> FakeStreamCueRepository:
    store = FakeStreamCueRepository()
    store.file_hashes[FILE] = AUDIO
    return store


def events(logs: list[dict[str, object]], name: str) -> list[dict[str, object]]:
    return [e for e in logs if e["event"] == name]


@pytest.fixture
def rig(tmp_path: Path) -> CueRig:
    return CueRig(tmp_path)


# --- The verdict (autocue_points) --------------------------------------------------------------


def test_the_stub_reads_as_silent_before_its_cue_points_are_checked() -> None:
    """The stub's cue points fail CuePoints (start-next equals the span); the gain is read
    first, so the verdict is silence, not that error."""
    result = autocue.autocue_points(SILENT_STUB)
    assert isinstance(result, autocue.Silent)
    assert result.reason == SILENT


def test_silence_with_valid_cue_points_is_still_silent() -> None:
    """S4b: the verdict keys on the gain alone. Silent audio can also come back with valid
    full-length cues (3 analysed rows on dev, which air today at +52 dB of gain)."""
    assert isinstance(autocue.autocue_points(SILENT_FULL), autocue.Silent)


@pytest.mark.parametrize("gain", ["52.0 dB", "52 dB", "52.000 dB"])
def test_the_floor_gain_is_silent_whatever_its_decimals(gain: str) -> None:
    """liq_amplify is a string; the check compares the parsed number, not the text."""
    assert isinstance(autocue.autocue_points({**SILENT_STUB, "liq_amplify": gain}), autocue.Silent)


def test_the_threshold_follows_the_autocue_target() -> None:
    """The threshold is computed from autocue.liq's target ("-18.", Liquidsoap's float literal)
    and the meter's floor; this fails if the two copies of the target disagree."""
    [raw] = _TARGET_LINE.findall(AUTOCUE_SETTINGS.read_text(encoding="utf-8"))
    assert float(raw) == autocue.AUTOCUE_LUFS_TARGET
    assert autocue.METER_FLOOR_LUFS == -70.0
    assert autocue.SILENT_GAIN_DB == autocue.AUTOCUE_LUFS_TARGET - autocue.METER_FLOOR_LUFS


def test_the_check_reads_the_computed_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """No bare 52 in the comparison: moving the threshold moves the verdict."""
    monkeypatch.setattr(autocue, "SILENT_GAIN_DB", 50.0)
    at = autocue.autocue_points({**SILENT_STUB, "liq_amplify": "50.0 dB"})
    below = autocue.autocue_points({**SILENT_STUB, "liq_amplify": "49.9 dB"})
    assert isinstance(at, autocue.Silent)
    assert not isinstance(below, autocue.Silent)


# --- The record (record_analysis) --------------------------------------------------------------


def test_the_stub_is_recorded_silent_with_the_fallback_values() -> None:
    """Proposal test 1: D52's fallback row, analysis_failed and audio_silent; one
    stream_cue_analysis_silent warning naming the file; no stream_cue_analysis_failed."""
    store = store_with_file()
    with capture_logs() as logs:
        record = record_analysis(store, StreamTiming(), Autocued(candidate(), SILENT_STUB))
    assert record == CueRecord.SILENT
    assert store.analyses == {
        AUDIO: CueAnalysis(
            audio_hash=AUDIO,
            cues=FALLBACK,
            loudness_lufs=None,
            analysis_failed=True,
            analyser_version=CUE_ANALYSER_VERSION,
            audio_silent=True,
        )
    }
    [event] = events(logs, "stream_cue_analysis_silent")
    assert event["log_level"] == "warning"
    assert (event["file_id"], event["path"], event["audio_hash"], event["reason"]) == (
        str(FILE),
        "D:/Music/silent.flac",
        str(AUDIO),
        SILENT,
    )
    assert events(logs, "stream_cue_analysis_failed") == []


@pytest.mark.parametrize(
    ("metadata", "points"),
    [
        ({**VAN_HALEN, "liq_amplify": "51.9 dB"}, replace(VAN_HALEN_POINTS, gain_db=51.9)),
        (TESTIMONY, TESTIMONY_POINTS),
    ],
    ids=["just below the floor", "Testimony (4 s of sound in 299 s)"],
)
def test_a_quiet_song_is_not_silent(metadata: dict[str, str], points: CuePoints) -> None:
    """Proposal test 2: any audible stretch is cued as now, however short or quiet."""
    store = store_with_file()
    with capture_logs() as logs:
        record = record_analysis(store, StreamTiming(), Autocued(candidate(), metadata))
    assert record == CueRecord.ANALYSED
    assert store.analyses[AUDIO].cues == points
    assert store.analyses[AUDIO].audio_silent is False
    assert events(logs, "stream_cue_analysis_silent") == []


def test_a_quiet_stub_is_a_failed_analysis_not_a_silent_one() -> None:
    """Below the floor the stub is an ordinary failure (D52, D68): a fallback row, not silent,
    logged as stream_cue_analysis_failed."""
    store = store_with_file()
    quiet_stub = {**SILENT_STUB, "liq_amplify": "51.9 dB"}
    with capture_logs() as logs:
        record = record_analysis(store, StreamTiming(), Autocued(candidate(), quiet_stub))
    assert record == CueRecord.FAILED
    assert (store.analyses[AUDIO].analysis_failed, store.analyses[AUDIO].audio_silent) == (
        True,
        False,
    )
    assert len(events(logs, "stream_cue_analysis_failed")) == 1
    assert events(logs, "stream_cue_analysis_silent") == []


def test_a_silent_file_without_a_duration_stores_nothing() -> None:
    """D55 unchanged: no duration, no fallback row to mark; retried every run, so logged
    quietly."""
    store = store_with_file()
    with capture_logs() as logs:
        record = record_analysis(store, StreamTiming(), Autocued(candidate(None), SILENT_STUB))
    assert (record, store.analyses) == (CueRecord.NO_DURATION, {})
    assert [e["log_level"] for e in logs if "stream_cue" in str(e["event"])] == ["debug"]


def test_nothing_is_stored_or_logged_when_the_hash_changed_meanwhile() -> None:
    """For PR D and PR E: nothing stored if the hash changed; the silent verdict is logged
    only when its row lands, so it is logged once, by the run that stores it."""
    store = FakeStreamCueRepository()
    store.file_hashes[FILE] = None
    with capture_logs() as logs:
        record = record_analysis(store, StreamTiming(), Autocued(candidate(), SILENT_STUB))
    assert (record, store.analyses) == (CueRecord.CHANGED, {})
    assert [e for e in logs if e["log_level"] == "warning"] == []


# --- The run (run_cue_analysis, analyse_reported) ------------------------------------------------


def test_a_silent_file_is_not_analysed_again_and_is_logged_once(rig: CueRig) -> None:
    """Proposal test 4 (D61): the silent row settles the audio; the next run tries nothing."""
    silent = rig.add("silent.flac")
    rig.analyser.results = {silent.path: SILENT_STUB}
    rig.run()
    again = rig.run()
    assert rig.analyser.order() == [silent.path]
    assert again is None
    assert rig.store.analyses[silent.audio_hash].audio_silent is True
    assert len(events(rig.logs, "stream_cue_analysis_silent")) == 1


def test_the_run_summary_counts_silent_files_apart_from_failed(rig: CueRig) -> None:
    """Proposal test 5, S5: the stream_cue_run summary has a silent count of its own."""
    silent = rig.add("silent.flac")
    failed = rig.add("failed.flac")
    rig.add("song.flac")
    rig.analyser.results = {silent.path: SILENT_STUB, failed.path: {}}
    summary = rig.run()
    assert summary is not None
    assert summary["log_level"] == "info"
    assert {k: summary[k] for k in ("analysed", "failed", "silent")} == {
        "analysed": 1,
        "failed": 1,
        "silent": 1,
    }


@dataclass
class _Sink:
    """A progress sink (CueRunProgress) that keeps what the run told it."""

    stored: list[int] = field(default_factory=list)
    ended: list[bool] = field(default_factory=list)

    def batch_stored(self, count: int) -> None:
        self.stored.append(count)

    def run_ended(self, *, failed: bool) -> None:
        self.ended.append(failed)


def test_a_silent_file_is_processed_and_failed_on_the_cue_meter(rig: CueRig) -> None:
    """D89a unchanged: the meter's X counts processed audio (analysed + failed), and a silent
    row is a failed row; the Streaming page's failed count includes it."""
    silent = rig.add("silent.flac")
    failed = rig.add("failed.flac")
    song = rig.add("song.flac")
    rig.analyser.results = {silent.path: SILENT_STUB, failed.path: {}}
    sink = _Sink()
    ports = CueRunPorts(
        work=rig.work, store=rig.store, commit=lambda: None, analyse=rig.analyser, progress=sink
    )
    run_cue_analysis(ports, CueRunConfig(today=TODAY, steady=lambda: 0.0))
    assert rig.store.analyses[silent.audio_hash].audio_silent is True
    assert (sum(sink.stored), sink.ended) == (3, [False])
    coverage = FakeCueCoverageRepository(rig.store)
    for added in (silent, failed, song):
        coverage.add(added.file_id, added.audio_hash)
    assert coverage.coverage() == CueCoverage(analysable=3, ready=1, failed=2, unhashed=0)


def test_a_reported_silent_file_is_stored_silent(rig: CueRig) -> None:
    """D79: a reported file is analysed as a run's batch would; its outcome is silent."""
    reported = rig.add("reported.flac")
    rig.analyser.results = {reported.path: SILENT_STUB}
    ports = CueRunPorts(work=rig.work, store=rig.store, commit=lambda: None, analyse=rig.analyser)
    with capture_logs() as logs:
        analyse_reported(ports, StreamTiming(), reported.file_id)
    assert rig.store.analyses[reported.audio_hash].audio_silent is True
    [done] = events(logs, "stream_cue_request_done")
    assert (done["log_level"], done["outcome"]) == ("info", "silent")
