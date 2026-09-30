"""The one cue write path.

Spec: For PR D and PR E ("reads the hash before analysing and stores nothing if the hash
changed meanwhile"); D52 (the fallback row: cue_in 0, cue_out = duration_ms, start_next 0,
default fades, gain_db -8, analysis_failed); D53 (loudness_lufs NULL); D54 (start_next_ms =
liq_cross_end_duration); D55 (a failure with no duration_ms stores nothing and is retried
every run, so it is logged quietly); D68 (a malformed result line fails only its file, with
a fallback row and the field named).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

import structlog

from backend.domain.streaming import (
    CUE_ANALYSER_VERSION,
    CueAnalysis,
    CueCandidate,
    CuePoints,
    StreamTiming,
)
from backend.repositories.stream_cues import StreamCueRepository
from backend.services.streaming.autocue import Unusable, autocue_points

logger = structlog.get_logger()

FALLBACK_GAIN_DB = -8.0
"""D52: the fallback row's gain, in the absence of a measured one."""

FALLBACK_START_NEXT_MS = 0
"""D52: the fallback row has no overlap with the next item."""


class CueRecord(StrEnum):
    """What ``record_analysis`` did with one candidate."""

    ANALYSED = "analysed"
    FAILED = "failed"
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


def record_analysis(
    store: StreamCueRepository, timing: StreamTiming, autocued: Autocued
) -> CueRecord:
    """Analyse one candidate and write the one result that matters (D20): the cues, or a
    fallback row, under the audio hash read before analysing."""
    candidate = autocued.candidate
    if isinstance(autocued.metadata, Unusable):
        result: CuePoints | Unusable = autocued.metadata
    else:
        result = autocue_points(autocued.metadata)
    if isinstance(result, Unusable):
        fallback = fallback_points(candidate.duration_ms, timing)
        if fallback is None:
            logger.debug(
                "stream_cue_analysis_failed_no_duration",
                file_id=str(candidate.file_id),
                path=candidate.path,
                reason=result.reason,
            )
            return CueRecord.NO_DURATION
        logger.warning(
            "stream_cue_analysis_failed",
            file_id=str(candidate.file_id),
            path=candidate.path,
            audio_hash=str(candidate.audio_hash),
            reason=result.reason,
        )
        points, failed = fallback, True
    else:
        points, failed = result, False
    analysis = CueAnalysis(candidate.audio_hash, points, None, failed, CUE_ANALYSER_VERSION)
    if not store.store_if_current(analysis, candidate.file_id):
        return CueRecord.CHANGED
    return CueRecord.FAILED if failed else CueRecord.ANALYSED
