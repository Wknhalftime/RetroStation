from abc import ABC, abstractmethod

from backend.domain.streaming import CueCoverage


class CueCoverageRepository(ABC):
    """Cue coverage (D77, D89; PG7): how much of the analysable audio has cues."""

    @abstractmethod
    def coverage(self) -> CueCoverage:
        """The current cue coverage, counted over the library now (D20)."""
        ...
