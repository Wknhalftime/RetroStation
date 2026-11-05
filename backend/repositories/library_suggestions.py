"""Port for what the Library asks the curator to look at, ``library_suggestions`` (migration 0047;
phase 2a S13, S14; C21, design 6.10). Its page comes in phase 5; until then nothing changes a
row's state, and the rows wait.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence

from backend.domain.enums import SuggestionReason
from backend.domain.library import LibrarySuggestion


class LibrarySuggestionRepository(ABC):
    @abstractmethod
    def upsert_for_name(
        self,
        normalized_name: str,
        reason: SuggestionReason,
        candidates: Sequence[str],
        evidence: Mapping[str, object],
    ) -> bool:
        """The one row naming no file for this name and reason: a new one is open, and a repeat
        updates candidates and evidence, never the state (a name the curator kept separate never
        reopens). Whether it wrote: the same thing again writes nothing."""
        ...

    @abstractmethod
    def get_for_name(
        self, normalized_name: str, reason: SuggestionReason
    ) -> LibrarySuggestion | None:
        """The row naming no file for this name and reason, if any."""
        ...
