from abc import ABC, abstractmethod

from backend.domain.streaming import CueAnalysis


class StreamCueRepository(ABC):
    """Stores one cue analysis per audio (``AudioHash``) for tune-in streaming (D20)."""

    @abstractmethod
    def upsert(self, analysis: CueAnalysis) -> None:
        """Store ``analysis`` as its audio's cues, replacing any earlier analysis."""
        ...
