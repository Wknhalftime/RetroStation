"""The one cue write path.

Spec: For PR D and PR E ("reads the hash before analysing and stores nothing if the hash
changed meanwhile"); D52 (the fallback row: cue_in 0, cue_out = duration_ms, start_next 0,
default fades, gain_db -8, analysis_failed); D53 (loudness_lufs NULL); D55 (a failure with
no duration_ms stores nothing and is retried every run); D68 (a malformed result line
fails only its file, with a fallback row and the field named); review minor "repeated failure
alerts" (a retried failure must not flood system_logs).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from backend.domain.library import AudioHash
from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    CueAnalysis,
    CueCandidate,
    CuePoints,
    StreamTiming,
)
from backend.services.streaming.autocue import Unusable
from backend.services.streaming.cue_record import Autocued, CueRecord, record_analysis
from tests.fakes.stream_cues import FakeStreamCueRepository
from tests.services.streaming.test_autocue import VAN_HALEN, VAN_HALEN_POINTS

AUDIO = AudioHash.parse("flac-md5:" + "0123456789abcdef" * 2)
FILE = uuid4()


def candidate(duration_ms: int | None = 200_000) -> CueCandidate:
    return CueCandidate(
        file_id=FILE,
        path="D:/Music/a.flac",
        audio_hash=AUDIO,
        duration_ms=duration_ms,
        file_size=1,
        file_mtime_ns=2,
    )


def store_with_file() -> FakeStreamCueRepository:
    store = FakeStreamCueRepository()
    store.file_hashes[FILE] = AUDIO
    return store


def test_an_analysis_is_stored_under_the_audio_hash_read_before_analysing() -> None:
    """Cue pre-computation upserts stream_cues keyed by the audio (D20); loudness NULL (D53)."""
    store = store_with_file()
    record = record_analysis(store, StreamTiming(), Autocued(candidate(), VAN_HALEN))
    assert record == CueRecord.ANALYSED
    assert store.analyses == {
        AUDIO: CueAnalysis(
            audio_hash=AUDIO,
            cues=VAN_HALEN_POINTS,
            loudness_lufs=None,
            analysis_failed=False,
            analyser_version=CUE_ANALYSER_VERSION,
        )
    }


def test_a_failed_analysis_stores_a_fallback_row() -> None:
    """D52: cue_in 0, cue_out = duration_ms, start_next 0, default fades, -8 dB, failed."""
    store = store_with_file()
    record = record_analysis(store, StreamTiming(), Autocued(candidate(200_000), {}))
    assert record == CueRecord.FAILED
    assert store.analyses == {
        AUDIO: CueAnalysis(
            audio_hash=AUDIO,
            cues=CuePoints(
                cue_in_ms=0,
                cue_out_ms=200_000,
                fade_in_ms=3_000,
                fade_out_ms=4_000,
                start_next_ms=0,
                gain_db=-8.0,
            ),
            loudness_lufs=None,
            analysis_failed=True,
            analyser_version=CUE_ANALYSER_VERSION,
        )
    }


def test_the_fallback_fades_are_the_stream_timing_defaults() -> None:
    """Spec: "Fades come from the cached cue points, or from StreamTiming defaults"."""
    store = store_with_file()
    timing = StreamTiming(default_fade_in_ms=1_000, default_fade_out_ms=2_000)
    record_analysis(store, timing, Autocued(candidate(), {**VAN_HALEN, "liq_cue_in": "x"}))
    cues = store.analyses[AUDIO].cues
    assert (cues.fade_in_ms, cues.fade_out_ms) == (1_000, 2_000)


def test_a_failed_analysis_is_logged_with_the_file_and_the_reason() -> None:
    """House rule: validation names the field; the log names the file (once: the fallback
    row means it is not analysed again)."""
    store = store_with_file()
    without_gain = {k: v for k, v in VAN_HALEN.items() if k != "liq_amplify"}
    with capture_logs() as logs:
        record_analysis(store, StreamTiming(), Autocued(candidate(), without_gain))
    [event] = [e for e in logs if e["event"] == "stream_cue_analysis_failed"]
    assert event["log_level"] == "warning"
    assert (event["file_id"], event["path"]) == (str(FILE), "D:/Music/a.flac")
    assert "liq_amplify" in event["reason"]


def test_a_rejected_result_line_stores_a_fallback_row_logging_its_reason() -> None:
    """D68: a malformed result line fails only its file: a fallback row (as D61), and the
    logged reason names the field."""
    store = store_with_file()
    rejected = Unusable("index on output line 9 is 5, but the file begun is 0")
    with capture_logs() as logs:
        record = record_analysis(store, StreamTiming(), Autocued(candidate(), rejected))
    assert (record, store.analyses[AUDIO].analysis_failed) == (CueRecord.FAILED, True)
    [event] = [e for e in logs if e["event"] == "stream_cue_analysis_failed"]
    assert event["log_level"] == "warning"
    assert "index" in event["reason"]


@pytest.mark.parametrize("duration_ms", [None, 0, -5])
def test_a_failed_analysis_without_a_duration_stores_nothing(duration_ms: int | None) -> None:
    """D55: no duration, no fallback row, and no skip list: it is retried every run. A
    duration of 0 or less gives no valid cue-out either."""
    store = store_with_file()
    record = record_analysis(store, StreamTiming(), Autocued(candidate(duration_ms), {}))
    assert (record, store.analyses) == (CueRecord.NO_DURATION, {})


def test_a_failure_retried_every_run_is_logged_quietly() -> None:
    """Review minor: a D55 retry happens every 5 minutes, so it is logged at debug, not as a
    warning in System Logs every run."""
    with capture_logs() as logs:
        record_analysis(store_with_file(), StreamTiming(), Autocued(candidate(None), {}))
    assert [e["log_level"] for e in logs if "stream_cue" in str(e["event"])] == ["debug"]


@pytest.mark.parametrize("metadata", [VAN_HALEN, {}], ids=["analysed", "failed with a duration"])
def test_nothing_is_stored_when_the_hash_changed_meanwhile(metadata: dict[str, str]) -> None:
    """For PR D and PR E: "stores nothing if the hash changed meanwhile" (here a retag
    cleared it until the backfill recomputes it)."""
    store = FakeStreamCueRepository()
    store.file_hashes[FILE] = None
    record = record_analysis(store, StreamTiming(), Autocued(candidate(), metadata))
    assert (record, store.analyses) == (CueRecord.CHANGED, {})
