"""Acceptance tests: the ``CueAnalysis`` write model (spec: Data, Cue pre-computation, D20).

Cues belong to the audio, not the file: an analysis names the library's ``AudioHash`` and
carries no file identity and no file stat. ``analyser_version`` is recorded, never checked.

DRAFT for D20: replaces ``test_cue_analysis.py`` once the user approves.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from backend.domain import streaming
from backend.domain.library import AudioHash
from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    CueAnalysis,
    CuePoints,
    InvalidStreamValueError,
)

CUES = CuePoints(
    cue_in_ms=1_000,
    cue_out_ms=181_000,
    fade_in_ms=2_000,
    fade_out_ms=5_000,
    start_next_ms=4_000,
    gain_db=-3.5,
)
AUDIO = AudioHash.parse("flac-md5:" + "0123456789abcdef" * 2)


def make(**overrides: Any) -> CueAnalysis:
    fields: dict[str, Any] = {
        "audio_hash": AUDIO,
        "cues": CUES,
        "loudness_lufs": -14.5,
        "analysis_failed": False,
        "analyser_version": 1,
    }
    return CueAnalysis(**{**fields, **overrides})


def test_keeps_its_fields() -> None:
    a = make()
    assert (a.audio_hash, a.cues, a.loudness_lufs, a.analysis_failed, a.analyser_version) == (
        AUDIO,
        CUES,
        -14.5,
        False,
        1,
    )


def test_is_keyed_by_the_audio_alone() -> None:
    """No file identity and no file stat: the analysis describes the audio (D20)."""
    assert [f.name for f in dataclasses.fields(CueAnalysis)] == [
        "audio_hash",
        "cues",
        "loudness_lufs",
        "analysis_failed",
        "analyser_version",
    ]


def test_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        make().analyser_version = 2  # type: ignore[misc]


def test_unknown_loudness_is_allowed() -> None:
    assert make(loudness_lufs=None).loudness_lufs is None


def test_failed_analysis_is_plain_data() -> None:
    a = make(analysis_failed=True)
    assert a.analysis_failed is True
    assert a.cues == CUES


@pytest.mark.parametrize("version", [0, -1])
def test_rejects_analyser_version_below_one(version: int) -> None:
    with pytest.raises(InvalidStreamValueError, match=r"CueAnalysis\.analyser_version"):
        make(analyser_version=version)


@pytest.mark.parametrize("lufs", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_loudness(lufs: float) -> None:
    with pytest.raises(InvalidStreamValueError, match=r"CueAnalysis\.loudness_lufs"):
        make(loudness_lufs=lufs)


def test_current_analyser_version_is_a_valid_version() -> None:
    assert isinstance(CUE_ANALYSER_VERSION, int)
    assert make(analyser_version=CUE_ANALYSER_VERSION).analyser_version >= 1


def test_cue_file_not_found_error_is_withdrawn() -> None:
    """Upserting needs no file (D20), so there is no 'file not found' failure to raise."""
    assert not hasattr(streaming, "CueFileNotFoundError")
