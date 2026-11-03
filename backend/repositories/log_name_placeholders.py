"""Port for the not-an-artist list, ``log_name_placeholders`` (migration 0045; C7, phase 1c S6).

The list holds the log names that are not artists by their exact cleaned name
(``broadcast_artists.normalized_name``): "38 special" is a real act, "special programming" is
not. Migration 0045 seeds the five station names; the curator adds and removes names with Not
an artist. A listed name is never linked or matched, and no run song-matches its lines.
"""

from abc import ABC, abstractmethod
from collections.abc import Collection

from backend.domain.enums import PlaceholderSource


class LogNamePlaceholderRepository(ABC):
    @abstractmethod
    def listed(self, normalized_names: Collection[str]) -> set[str]:
        """Which of *normalized_names* are on the list, compared exactly."""
        ...

    @abstractmethod
    def add(self, normalized_name: str, added_by: PlaceholderSource) -> bool:
        """Put the name on the list; whether it was added (a listed name keeps its row)."""
        ...

    @abstractmethod
    def remove(self, normalized_name: str) -> bool:
        """Take the name off the list; whether it was listed."""
        ...
