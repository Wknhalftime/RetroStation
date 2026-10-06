"""D110 downstream: a file rejected for a missing result line takes D68's path (user,
2026-10-05; refines D65 and D68).

The file gets a fallback row (D52) that is not retried until the analyser version changes
(D61); the existing ``stream_cue_analysis_failed`` warning, once for that file, is the only
signal, its reason beginning ``no RS_CUE_RESULT for file``. The other files of the batch are
analysed. The analyser itself is faked here; tests/playout/test_cue_analysis_missing_result.py
covers how it reports the missing line.
"""

from __future__ import annotations

from pathlib import Path

from backend.services.streaming.cue_record import CueRecord
from tests.services.streaming.cue_library import CueRig

MISSING = "no RS_CUE_RESULT for file 1 before RS_CUE_BEGIN 2 on output line 5"


def test_a_missing_result_stores_a_fallback_row_for_that_file_only(tmp_path: Path) -> None:
    rig = CueRig(tmp_path)
    first, missing, last = (rig.add(name) for name in ("a.flac", "b.flac", "c.flac"))
    rig.analyser.rejects = {missing.path: MISSING}
    summary = rig.run()
    assert summary is not None
    assert (summary[CueRecord.ANALYSED], summary[CueRecord.FAILED]) == (2, 1)
    assert rig.store.analyses[missing.audio_hash].analysis_failed is True
    assert not rig.store.analyses[first.audio_hash].analysis_failed
    assert not rig.store.analyses[last.audio_hash].analysis_failed


def test_the_warning_once_for_that_file_is_the_only_signal(tmp_path: Path) -> None:
    rig = CueRig(tmp_path)
    rig.add("a.flac")
    missing = rig.add("b.flac")
    rig.analyser.rejects = {missing.path: MISSING}
    rig.run()
    warnings = [e for e in rig.logs if e["log_level"] not in ("debug", "info")]
    assert [(e["event"], e["path"], e["reason"]) for e in warnings] == [
        ("stream_cue_analysis_failed", missing.path, MISSING)
    ]


def test_the_fallback_row_is_not_retried_by_the_next_run(tmp_path: Path) -> None:
    rig = CueRig(tmp_path)
    missing = rig.add("b.flac")
    rig.analyser.rejects = {missing.path: MISSING}
    rig.run()
    rig.analyser.rejects = {}
    rig.run()
    assert rig.analyser.order() == [missing.path]
    assert rig.store.analyses[missing.audio_hash].analysis_failed is True
