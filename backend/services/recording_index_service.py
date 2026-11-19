"""The global recording index, built once no recording MBID is under two works (work merges
phase 3b PR 4; SPEC S5, E6; design C40; AUD-R080)."""

from __future__ import annotations

from backend.domain.performances import (
    RecordingIndexDuplicateError,
    RecordingIndexOutcome,
    RecordingIndexState,
)
from backend.repositories.performances import PerformanceRepository
from backend.repositories.recording_index import RecordingIndexRepository


def _remaining(performances: PerformanceRepository) -> tuple[str, ...]:
    return tuple(d.recording_mbid for d in performances.duplicate_recordings())


def build_when_unique(
    performances: PerformanceRepository, index: RecordingIndexRepository
) -> RecordingIndexOutcome:
    """Wait while any recording MBID is under two works (E6); else build the index, once. An
    invalid index (a failed build) is dropped and built again."""
    remaining = _remaining(performances)
    if remaining:
        return RecordingIndexOutcome(done=False, built=False, remaining=remaining)
    state = index.state()
    if state is RecordingIndexState.VALID:
        return RecordingIndexOutcome(done=True, built=False, remaining=())
    if state is RecordingIndexState.INVALID:
        index.drop()
    try:
        index.build()
    except RecordingIndexDuplicateError:
        index.drop()
        return RecordingIndexOutcome(done=False, built=False, remaining=_remaining(performances))
    return RecordingIndexOutcome(done=True, built=True, remaining=())
