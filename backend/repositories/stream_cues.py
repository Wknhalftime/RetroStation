from abc import ABC, abstractmethod

from backend.domain.streaming import CueAnalysis


class StreamCueRepository(ABC):
    """Stores one cue analysis per library file for tune-in streaming."""

    @abstractmethod
    def upsert(self, analysis: CueAnalysis) -> None:
        """Store ``analysis`` as its file's cues, replacing any earlier analysis."""
        ...
