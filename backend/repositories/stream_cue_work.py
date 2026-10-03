from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import date
from uuid import UUID

from backend.domain.streaming import CueCandidate


class CueWorkRepository(ABC):
    """What cue pre-computation reads: audio that needs analysis (D20): a present file with
    an audio_hash whose audio has no stream_cues row, and the file a listener heard without
    cues."""

    @abstractmethod
    def logged_years(self) -> range:
        """From the first to the last logged year, on the UTC-label date (D3); empty when
        there are no plays."""
        ...

    @abstractmethod
    def scheduled(self, days: Sequence[date]) -> list[CueCandidate]:
        """The needing-analysis final files of the plays on ``days`` at every station, each
        file once, in path order. Read once per run (review I4)."""
        ...

    @abstractmethod
    def library(self, after_path: str | None, limit: int) -> list[CueCandidate]:
        """Every needing-analysis file, in path order after ``after_path``."""
        ...

    @abstractmethod
    def reported(self, file_id: UUID) -> CueCandidate | None:
        """The reported file as a candidate while its audio needs analysis (D20, D79); None
        when its audio has a row (cues or a failed row, D61), it has no hash, it is not
        present (D21), or it is unknown."""
        ...
