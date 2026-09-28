"""Acceptance tests: the ``CueAnalysis`` write model (spec: Data, Cue pre-computation)."""

from __future__ import annotations

import dataclasses
from typing import Any
from uuid import uuid4

import pytest

from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    CueAnalysis,
    CueFileNotFoundError,
    CuePoints,
    InvalidStreamValueError,
    StreamingError,
)

CUES = CuePoints(
    cue_in_ms=1_000,
    cue_out_ms=181_000,
    fade_in_ms=2_000,
    fade_out_ms=5_000,
    start_next_ms=4_000,
    gain_db=-3.5,
)
FILE_ID = uuid4()


def make(**overrides: Any) -> CueAnalysis:
    fields: dict[str, Any] = {
        "file_id": FILE_ID,
        "cues": CUES,
        "loudness_lufs": -14.5,
        "analysis_failed": False,
        "analyser_version": 1,
        "file_size": 4_000_000,
        "file_mtime_ns": 800_000_000_000_000_000,
    }
    return CueAnalysis(**{**fields, **overrides})


def test_keeps_its_fields() -> None:
    a = make()
    assert (
        a.file_id,
        a.cues,
        a.loudness_lufs,
        a.analysis_failed,
        a.analyser_version,
        a.file_size,
        a.file_mtime_ns,
    ) == (FILE_ID, CUES, -14.5, False, 1, 4_000_000, 800_000_000_000_000_000)


def test_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        make().analyser_version = 2  # type: ignore[misc]


def test_unknown_loudness_and_stat_are_allowed() -> None:
    a = make(loudness_lufs=None, file_size=None, file_mtime_ns=None)
    assert (a.loudness_lufs, a.file_size, a.file_mtime_ns) == (None, None, None)


def test_failed_analysis_is_plain_data() -> None:
    a = make(analysis_failed=True)
    assert a.analysis_failed is True
    assert a.cues == CUES


@pytest.mark.parametrize("version", [0, -1])
def test_rejects_analyser_version_below_one(version: int) -> None:
    with pytest.raises(InvalidStreamValueError, match=r"CueAnalysis\.analyser_version"):
        make(analyser_version=version)


def test_rejects_negative_file_size() -> None:
    with pytest.raises(InvalidStreamValueError, match=r"CueAnalysis\.file_size"):
        make(file_size=-1)


@pytest.mark.parametrize("lufs", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_loudness(lufs: float) -> None:
    with pytest.raises(InvalidStreamValueError, match=r"CueAnalysis\.loudness_lufs"):
        make(loudness_lufs=lufs)


def test_current_analyser_version_is_a_valid_version() -> None:
    assert isinstance(CUE_ANALYSER_VERSION, int)
    assert make(analyser_version=CUE_ANALYSER_VERSION).analyser_version >= 1


def test_cue_file_not_found_is_a_streaming_error_naming_the_file() -> None:
    error = CueFileNotFoundError(FILE_ID)

    assert isinstance(error, StreamingError)
    assert error.file_id == FILE_ID
    assert f"file_id={FILE_ID}" in str(error)
