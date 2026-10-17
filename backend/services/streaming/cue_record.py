"""The one cue write path.

Spec: For PR D and PR E ("reads the hash before analysing and stores nothing if the hash
changed meanwhile"); D52 (the fallback row: cue_in 0, cue_out = duration_ms, start_next 0,
default fades, gain_db -8, analysis_failed); D53 (loudness_lufs NULL); D54 (start_next_ms =
liq_cross_end_duration); D55 (a failure with no duration_ms stores nothing and is retried
every run, so it is logged quietly); D68 (a malformed result line fails only its file, with
a fallback row and the field named); D111 (silent audio gets the fallback row marked
audio_silent, logged once as stream_cue_analysis_silent when it is stored, never as a failure).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import structlog

from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    CueAnalysis,
    CueCandidate,
    CuePoints,
    StreamTiming,
)
from backend.repositories.stream_cues import StreamCueRepository
from backend.services.streaming.autocue import Silent, Unusable, autocue_points

logger = structlog.get_logger()

FALLBACK_GAIN_DB = -8.0
"""D52: the fallback row's gain, in the absence of a measured one."""

FALLBACK_START_NEXT_MS = 0
"""D52: the fallback row has no overlap with the next item."""


class CueRecord(StrEnum):
    """What ``record_analysis`` did with one candidate."""

    ANALYSED = "analysed"
    FAILED = "failed"
    SILENT = "silent"
    NO_DURATION = "no_duration"
    CHANGED = "changed"


@dataclass(frozen=True)
class Autocued:
    """One candidate and what autocue said about it.

    ``metadata`` is the autocue.internal output to parse, or an ``Unusable`` already
    naming why it cannot be used (D68: a rejected result line) — that reason is used as
    is, so the log names the field.
    """

    candidate: CueCandidate
    metadata: Mapping[str, str] | Unusable


def fallback_points(duration_ms: int | None, timing: StreamTiming) -> CuePoints | None:
    """D52's fallback row for a file of ``duration_ms``; ``None`` when there is none to give
    (D55: no duration, no fallback, no stored row)."""
    if duration_ms is None or duration_ms <= 0:
        return None
    return CuePoints(
        cue_in_ms=0,
        cue_out_ms=duration_ms,
        fade_in_ms=timing.default_fade_in_ms,
        fade_out_ms=timing.default_fade_out_ms,
        start_next_ms=FALLBACK_START_NEXT_MS,
        gain_db=FALLBACK_GAIN_DB,
    )


def _verdict(autocued: Autocued) -> CuePoints | Silent | Unusable:
    """What autocue said: a rejected line's reason as is (D68), or its metadata read."""
    if isinstance(autocued.metadata, Unusable):
        return autocued.metadata
    return autocue_points(autocued.metadata)


def _warn(event: str, candidate: CueCandidate, reason: str) -> None:
    """One warning naming the file, its audio and the reason."""
    logger.warning(
        event,
        file_id=str(candidate.file_id),
        path=candidate.path,
        audio_hash=str(candidate.audio_hash),
        reason=reason,
    )


def _stored(
    store: StreamCueRepository, analysis: CueAnalysis, file_id: UUID, outcome: CueRecord
) -> CueRecord:
    """``outcome`` once ``analysis`` is stored under the hash read before analysing; CHANGED,
    with nothing stored, when the file no longer carries that hash."""
    if not store.store_if_current(analysis, file_id):
        return CueRecord.CHANGED
    return outcome


def record_analysis(
    store: StreamCueRepository, timing: StreamTiming, autocued: Autocued
) -> CueRecord:
    """Analyse one candidate and write the one result that matters (D20): the cues, or a
    fallback row (marked silent for silent audio, D111), under the audio hash read before
    analysing."""
    candidate = autocued.candidate
    verdict = _verdict(autocued)
    if isinstance(verdict, CuePoints):
        cued = CueAnalysis(candidate.audio_hash, verdict, None, False, CUE_ANALYSER_VERSION)
        return _stored(store, cued, candidate.file_id, CueRecord.ANALYSED)
    fallback = fallback_points(candidate.duration_ms, timing)
    if fallback is None:
        logger.debug(
            "stream_cue_analysis_failed_no_duration",
            file_id=str(candidate.file_id),
            path=candidate.path,
            reason=verdict.reason,
        )
        return CueRecord.NO_DURATION
    silent = isinstance(verdict, Silent)
    if not silent:
        _warn("stream_cue_analysis_failed", candidate, verdict.reason)
    row = CueAnalysis(
        candidate.audio_hash, fallback, None, True, CUE_ANALYSER_VERSION, audio_silent=silent
    )
    kept = CueRecord.SILENT if silent else CueRecord.FAILED
    outcome = _stored(store, row, candidate.file_id, kept)
    if outcome is CueRecord.SILENT:
        _warn("stream_cue_analysis_silent", candidate, verdict.reason)
    return outcome
