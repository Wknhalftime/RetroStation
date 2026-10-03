from abc import ABC, abstractmethod
from collections.abc import Callable

from backend.domain.streaming import CueCoverage

type CoverageRead = Callable[[], CueCoverage | None]
"""One bounded read of the cue coverage, on a connection of its own (I7): None when the read
failed or timed out, never an exception. The cue run's progress sink reads it (D89)."""


class CueCoverageRepository(ABC):
    """Cue coverage (D77, D89; PG7): how much of the analysable audio has cues."""

    @abstractmethod
    def coverage(self) -> CueCoverage:
        """The current cue coverage, counted over the library now (D20)."""
        ...
